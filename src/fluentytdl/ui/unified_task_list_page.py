"""
统一任务列表页面

导航里唯一的「任务」入口：正在下载的任务与历史记录是**同一条列表**（迅雷式），
行来源见 `ui/models/download_list_model.py` 的类注释。

顶部是 4 个主桶（全部 / 下载中 / 已完成 / 失败）+「更多筛选」下拉里的 5 项二级筛选，
桶 → 状态集合的映射是本模块的 `_FILTER_STATES`。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from PySide6.QtCore import (
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QListView,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    DotInfoBadge,
    DropDownToolButton,
    FluentIcon,
    InfoBadgeManager,
    InfoBar,
    InfoBarPosition,
    PillPushButton,
    RoundMenu,
    SubtitleLabel,
)

from ..utils.logger import logger
from .delegates.download_item_delegate import DownloadItemDelegate
from .models.download_list_model import DownloadListModel
from .models.task_row import TaskRow

# 主 pivot 的筛选项显示名在 `_init_ui` 里构建（`self._filter_labels`）——
# lupdate 只能从 self.tr("字面量") 提取待翻译串，所以那份映射不能挪到模块级。

# 「开始」可作用的状态：这些任务要么没跑、要么已经停了
_STARTABLE_STATES = frozenset({"paused", "queued", "error", "cancelled", "quality_guard"})
# 「暂停」可作用的状态
_PAUSABLE_STATES = frozenset({"running", "queued"})
# 这些状态下「开始」的语义是**重下**而不是继续，菜单项要跟着改叫「重试」——
# `download_card._retry_download` 那颗独立的重试按钮随卡片一起没了，但功能一直在
# 「开始」里（`controller.handle_pause_resume_task` 对已结束的线程会重建 worker，
# 没有 worker 的历史行走 `handle_start_snapshot`），缺的只是这个说法。
_RETRY_STATES = frozenset({"error", "cancelled"})
# 「降低画质重试」可作用的状态。只挂在 `error` 上：严格档位拿不到片源时 yt-dlp 直接
# 报错，任务落在 `error`。`quality_guard` 不算 —— 它是「整体质量异常过多」的风控挂起
# （`download_manager.suspend_pending`），跟这个视频有没有 1080p 无关，降档不是它的解法。
_DOWNGRADABLE_STATES = frozenset({"error"})

# === 筛选桶 ===
# 主 pivot 只留 4 桶（迅雷式）。running / queued / paused 在用户心里是同一桶
# 「还没下完的」，操作方式也相同，不该占三个 tab 位。
_MAIN_BUCKETS: tuple[str, ...] = ("all", "active", "completed", "failed")
# 二级筛选：降级到「更多筛选」下拉，功能一个都不删。选中时主 pivot 停在「全部」，
# 并在右侧显示一枚可关闭的 chip —— 否则用户会以为「我明明有任务却是空的」。
_SECONDARY_FILTERS: tuple[str, ...] = (
    "queued",
    "paused",
    "quality_guard",
    "cancelled",
    "missing",
)

# 桶 / 二级筛选 → 状态集合。**空集合 = 不按状态筛**（「全部」）。
#
# `downloading` / `parsing` 也在 `active` 里：`tasks.state` 这一列是
# `worker.effective_state` 的**超集** —— CleanLogger 会往库里写这两个中间态，而没有
# worker 的快照行直接读该列。漏掉它们，重启时正在解析的快照行会从「下载中」消失。
#
# `quality_guard` 同样算「下载中」：它是 `suspend_pending()` 把排队任务**冻住**的结果，
# 语义上就是「系统替你按了暂停」，任务一个都没下完。把它排除在三个主桶之外会让
# 「全部」比「下载中 + 已完成 + 失败」多出几条无处可去的行 —— 这正是旧的 7 Tab 设计
# 「又多又缺」的毛病。它降级进「更多筛选」只是**不再单独占一个 Tab**，不是从主桶里消失；
# 想单独看它仍然走二级筛选 + 「更多筛选」上的圆点徽标。
_FILTER_STATES: dict[str, frozenset[str]] = {
    "all": frozenset(),
    "active": frozenset(
        # `downloading` / `parsing` / `processing` 是 DB 独有的中间态：没有 worker 的
        # 快照行直接读 `tasks.state`，漏掉哪个，那种行就会从「下载中」里消失。
        # `processing`（FFmpeg 合并 / 嵌字幕 / 封面）是发射次数最多的状态，尤其不能漏。
        {"running", "downloading", "parsing", "processing", "queued", "paused", "quality_guard"},
    ),
    "completed": frozenset({"completed"}),
    "failed": frozenset({"error", "cancelled"}),
    "queued": frozenset({"queued"}),
    "paused": frozenset({"paused"}),
    "quality_guard": frozenset({"quality_guard"}),
    "cancelled": frozenset({"cancelled"}),
    # 「文件丢失」是 completed 的子集，额外要求 `file_exists is False`
    "missing": frozenset({"completed"}),
}

# 计数的两个来源必须**按状态分工，绝不相加**：
#
# * 非终态行**全部驻留在内存**里（启动时由 `load_unfinished_tasks()` 一次性注入），而且
#   会话内的状态迁移是先到 worker、再由异步 `db_writer` 落库 —— 模型既完整又更新；
# * 终态行模型里只有 `fetchMore()` 分页读进来的**前缀**，只有 DB 才知道总数。
#
# 两个集合不相交，所以同一行不会被数两次。极端情况下会**少算一个**（内存已经是
# completed 而 DB 还写着 downloading），下一个 tick 自愈。
#
# 直接由三个主桶推导，而不是另抄一份状态字面量：三个主桶恰好把 `tasks.state` 的取值
# 全集划分完（`tests/test_task_filter_buckets.py` 钉住了这条），所以「非终态 = 下载中桶」
# 是结构事实而非巧合。抄一份的话，往桶里加状态时这里会静默漏掉，计数就和 Tab 打架。
_MEMORY_COUNT_STATES = _FILTER_STATES["active"]
_DB_COUNT_STATES = _FILTER_STATES["completed"] | _FILTER_STATES["failed"]

# 计数刷新的合并窗口（毫秒）。`download_manager.task_updated` 在下载过程中是高频信号，
# 每次都去 `GROUP BY state` 显然不行。
_COUNTS_REFRESH_MS = 300

# 计数显示上限。四位数会把 tab 撑宽，进而让胶囊指示条的位置失准。
_COUNT_DISPLAY_MAX = 999

# 页面四周边距与顶栏行内间距。`_apply_responsive_header()` 要按这两个值算「还剩多少宽」，
# 所以它们必须是常量而不是散在 `setContentsMargins` / `setSpacing` 里的字面量 ——
# 改了边距却忘了改判定，降级阈值就会悄悄偏掉。
_PAGE_MARGIN = 20
_HEADER_SPACING = 8

PIVOT_DOT_BADGE_POSITION = "fluentytdl.pivotDotBadge"


def _sum_states(counts: dict[str, int], states: frozenset[str]) -> int:
    """按状态集合求和，缺失的键算 0。"""
    return sum(int(counts.get(state, 0) or 0) for state in states)


@InfoBadgeManager.register(PIVOT_DOT_BADGE_POSITION)
class _InsetDotBadgeManager(InfoBadgeManager):
    """把圆点徽标钉在目标右上角**内侧**。

    库里现成的 manager 都把徽标放到目标**外面**（`TopRightInfoBadgeManager` 拿
    `geometry().topRight()` 再减半个徽标宽），对 pivot 项来说那正好落在容器边界之外
    —— 实测圆点会被裁掉，而且没有任何现成 manager 会往父控件矩形里做钳制。
    写法与 `reimagined_main_window.TitleBarBadgeManager` 同源。
    """

    def position(self) -> QPoint:
        geo = self.target.geometry()
        x = geo.right() - self.badge.width()
        y = geo.top() + 2
        parent = self.badge.parent()
        if parent is not None:
            x = max(0, min(x, parent.width() - self.badge.width()))
            y = max(0, min(y, parent.height() - self.badge.height()))
        return QPoint(x, y)


class DownloadFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._filter = "all"
        self._states: frozenset[str] = frozenset()
        self._require_missing = False

    def set_filter(self, status: str):
        self._filter = status
        # 桶名 → 状态集合只解析一次。`filterAcceptsRow` 会被每一行、每次
        # `dataChanged` 各调一遍（`setDynamicSortFilter(True)`），不该在里面查字典。
        self._states = _FILTER_STATES.get(status, frozenset())
        self._require_missing = status == "missing"
        # 用公开的 `invalidate()`：`invalidateFilter()` / `invalidateRowsFilter()` /
        # `invalidateColumnsFilter()` 在 C++ 侧是 protected，PySide6 6.10 把这三个全标了
        # deprecated（实测 `-W error::DeprecationWarning` 下三个都抛，只有 `invalidate()`
        # 不抛）。代价只是顺带重排一次序，而筛选切换本来就要重算可见集。
        self.invalidate()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        # 空集合 = 不按状态筛。未知桶名也走这条 —— 「筛选没生效」远好过「列表整个空掉」。
        if not self._states:
            return True

        model = self.sourceModel()
        if not hasattr(model, "get_task"):
            return True

        row_obj = model.get_task(source_row)
        if row_obj is None:
            return False

        # `effective_state` 对两种行来源都成立：有 worker 问 worker，没有就读 DB 快照。
        # 旧实现在 `worker` 为空时直接 return False —— 融合后那会让所有历史行
        # 从「全部」以外的每一个页签里消失。
        if row_obj.effective_state not in self._states:
            return False

        if self._require_missing:
            # `file_exists` 三态：None = 还没查（按存在渲染），所以只认明确的 False。
            # 结论是 `FileExistenceProbe` 流式回来的，列表会渐进填充。
            return row_obj.file_exists is False

        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        # 因为在 DownloadListModel 中新任务始终插入在 row=0，
        # 所以 row 越小，任务越新。按照 row 的大小排序即可代表添加时间的先后。
        return left.row() < right.row()


class UnifiedTaskListPage(QWidget):
    """
    统一任务列表页面

    特性:
    - 单一 ScrollArea 容纳所有任务卡片
    - Pivot 顶部过滤器切换显示
    - 空状态占位符
    - 新任务插入顶部
    """

    # Signals: User actions from delegate
    card_remove_requested = Signal(int)
    card_resume_requested = Signal(int)
    card_folder_requested = Signal(int)

    # 批量动作。参数是 **source** 行号列表，MainWindow 侧的签名因此保持不变。
    batch_start_requested = Signal(list)
    batch_pause_requested = Signal(list)
    batch_delete_requested = Signal(list, bool)
    # 「降低画质重试」。严格画质档位（`bestvideo[height=N]`）遇到片源缺档会直接失败，
    # 这条信号把降档重试补回来 —— 它原先长在 `download_card` 的错误回调里，而那个
    # 卡片组件在列表虚拟化之后再没被实例化过。
    batch_downgrade_requested = Signal(list)

    # 右键菜单的「重新解析」。承接原历史页的同名信号 →
    # MainWindow.show_selection_dialog(url, smart_detect=True)
    reparse_requested = Signal(str)

    # 各桶计数（`bucket_counts()` 的返回值）。导航项上的活跃数徽标靠它刷新 ——
    # 页面自己有全部数据，MainWindow 不该再伸手进 model / task_db 数一遍。
    counts_changed = Signal(dict)

    # Navbar trigger
    route_to_parse = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("unifiedTaskListPage")

        self.model = DownloadListModel(self)
        self.proxy_model = DownloadFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.model)
        self.proxy_model.setDynamicSortFilter(True)
        self.proxy_model.sort(0, Qt.SortOrder.AscendingOrder)

        self.delegate = DownloadItemDelegate(self)

        self._current_filter: str = "all"

        # `Pivot.setCurrentItem()` 在真的换了项时会回发 `currentItemChanged`。二级筛选
        # 生效时我们要把 pivot 拨回「全部」，那条回声若不拦，就会用桶名把刚设好的
        # 二级筛选覆盖掉。
        self._syncing_pivot = False

        # 空状态刷新去抖：多条模型信号在同一轮事件循环里只会触发一次真正的检查。
        # 之前每条信号各排一个 QTimer.singleShot(0, ...) 闭包，批量加 N 个任务会排出 ~2N 个。
        self._empty_state_timer = QTimer(self)
        self._empty_state_timer.setSingleShot(True)
        self._empty_state_timer.setInterval(0)
        self._empty_state_timer.timeout.connect(self._apply_empty_state)

        # 计数刷新去抖。窗口比空状态那条长得多：数一次要一条 `GROUP BY state`。
        self._counts: dict[str, int] = {}
        self._counts_timer = QTimer(self)
        self._counts_timer.setSingleShot(True)
        self._counts_timer.setInterval(_COUNTS_REFRESH_MS)
        self._counts_timer.timeout.connect(self._recount)

        # 主 tab 已占住的宽度（routeKey -> px，只涨不落）。见 `_reserve_pivot_widths`。
        self._pivot_reserved: dict[str, int] = {}

        # `_apply_responsive_header()` 的重入闸。显隐一个控件会改变 `header_layout` 的
        # 最小宽，进而可能触发新的 resize/布局回调再调回来 —— 没有这道闸就会自激。
        self._adjusting_header = False
        self._header_label_collapsed = False
        # 顶栏降级判定的合并 + 延后。零延时：只是要等这一轮 posted 的 `LayoutRequest`
        # 派发完，理由见 `_schedule_responsive_header`。
        self._header_timer = QTimer(self)
        self._header_timer.setSingleShot(True)
        self._header_timer.setInterval(0)
        self._header_timer.timeout.connect(self._apply_responsive_header)

        self._init_ui()

        # Connect model changes to empty state updates.
        # 只接 proxy 的三条结构信号：proxy 会把 source 的插入/删除/重置转发过来，
        # source 端再接一遍纯属重复触发。
        # Note: dataChanged is NOT connected here — it only indicates
        # property changes on existing rows (e.g. progress) and never
        # affects row count, so it cannot change the empty/non-empty state.
        # Connecting it caused full-list flicker on every progress tick.
        self.proxy_model.rowsInserted.connect(self._update_empty_state)
        self.proxy_model.rowsRemoved.connect(self._update_empty_state)
        self.proxy_model.modelReset.connect(self._update_empty_state)

        # force initial empty state
        self._update_empty_state()

        # Delegate signals
        self.delegate.delete_clicked.connect(self._on_delegate_delete)
        self.delegate.pause_resume_clicked.connect(self._on_delegate_pause_resume)
        self.delegate.open_folder_clicked.connect(self._on_delegate_open_folder)
        self.delegate.selection_toggled.connect(self._on_delegate_selection)

        # Image Loader signals
        from ..utils.image_loader import get_image_loader

        get_image_loader().loaded_with_url.connect(self._on_image_loaded)

    def _on_image_loaded(self, url: str, pixmap: QPixmap) -> None:
        self.delegate.set_pixmap(url, pixmap)
        # Repaint only the rows whose thumbnail matches this url,
        # instead of invalidating the entire viewport.
        # 行号由模型的 url -> owners 索引直接给出（O(命中数)），并且同一封面被多行
        # 共用时每一行都会刷新 —— 旧实现遍历全部 proxy 行且 break，只刷第一行。
        if not hasattr(self, "list_view"):
            return
        for src_row in self.model.rows_for_thumbnail(url):
            proxy_idx = self.proxy_model.mapFromSource(self.model.index(src_row, 0))
            if proxy_idx.isValid():
                self.list_view.update(proxy_idx)

    def _on_delegate_delete(self, proxy_row: int):
        src_idx = self.proxy_model.mapToSource(self.proxy_model.index(proxy_row, 0))
        self.card_remove_requested.emit(src_idx.row())

    def _on_delegate_pause_resume(self, proxy_row: int):
        src_idx = self.proxy_model.mapToSource(self.proxy_model.index(proxy_row, 0))
        self.card_resume_requested.emit(src_idx.row())

    def _on_delegate_open_folder(self, proxy_row: int):
        src_idx = self.proxy_model.mapToSource(self.proxy_model.index(proxy_row, 0))
        self.card_folder_requested.emit(src_idx.row())

    def _on_delegate_selection(self, proxy_row: int):
        """复选框被点击 → 走 selectionModel 切换该行。

        选中态的唯一真值是 `QItemSelectionModel`，模型里不再存 `is_selected` ——
        存在模型里就会和 proxy 的筛选打架（旧的「全选」正是因此绕过了筛选）。
        """
        selection_model = self.list_view.selectionModel()
        if selection_model is None:
            return
        index = self.proxy_model.index(proxy_row, 0)
        if not index.isValid():
            return
        selection_model.select(
            index,
            QItemSelectionModel.SelectionFlag.Toggle | QItemSelectionModel.SelectionFlag.Rows,
        )
        # 同步键盘/Shift 连选的锚点，但不要动选中集合本身
        selection_model.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)

    def _init_ui(self) -> None:
        """初始化 UI 布局"""
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(_PAGE_MARGIN, _PAGE_MARGIN, _PAGE_MARGIN, _PAGE_MARGIN)
        self.v_layout.setSpacing(16)

        # === 标题 ===
        self.title_label = SubtitleLabel(self.tr("任务"), self)
        self.v_layout.addWidget(self.title_label)

        # === 用于筛选的 SegmentedWidget (胶囊样式) ===
        from PySide6.QtWidgets import QHBoxLayout
        from qfluentwidgets import ComboBox, SegmentedWidget

        from ..core.config_manager import config_manager

        self.header_layout = QHBoxLayout()
        self.header_layout.setContentsMargins(0, 0, 0, 0)
        # 显式定间距：默认间距由样式给（实测 8~9px），再叠上逐处 `addSpacing` 就成了
        # 「隐性预算」—— 这一行的控件本来就快摆不下，把 11 个间隙的宽度交给样式去决定
        # 等于把错叠的触发点交给样式。统一 8px，需要更大分组间隔的地方再显式加。
        self.header_layout.setSpacing(_HEADER_SPACING)

        self.pivot = SegmentedWidget(self)
        # routeKey -> 显示名。批量确认框要写明「作用域」，得能反查当前筛选的显示名。
        # 逐条写字面量而不是 self.tr(变量)：lupdate 只从字面量提取待翻译串，
        # 用变量会让 assets/locales/*.ts 下次同步时丢掉这几条。
        # 前 4 条上主 pivot（顺序即 `_MAIN_BUCKETS`），后 5 条降级到「更多筛选」。
        self._filter_labels: dict[str, str] = {
            "all": self.tr("全部"),
            "active": self.tr("下载中"),
            "completed": self.tr("已完成"),
            "failed": self.tr("失败"),
            "queued": self.tr("排队中"),
            "paused": self.tr("已暂停"),
            "quality_guard": self.tr("质量守卫"),
            "cancelled": self.tr("已取消"),
            "missing": self.tr("文件丢失"),
        }
        for route_key in _MAIN_BUCKETS:
            self.pivot.addItem(routeKey=route_key, text=self._filter_labels[route_key])
        self._reserve_pivot_widths()
        self.pivot.currentItemChanged.connect(self._on_pivot_changed)
        self.header_layout.addWidget(self.pivot)

        # === 「更多筛选」下拉 ===
        # 二级筛选不占 tab 位，但也不能让用户找不到：菜单项常驻，质量守卫 > 0 时
        # 按钮上挂一枚圆点徽标。
        self.more_filter_btn = DropDownToolButton(FluentIcon.FILTER, self)
        self.more_filter_btn.setToolTip(self.tr("更多筛选"))
        self._more_menu = RoundMenu(parent=self.more_filter_btn)
        self._more_actions: dict[str, Action] = {}
        for route_key in _SECONDARY_FILTERS:
            action = Action(self._filter_labels[route_key], self)
            action.triggered.connect(partial(self.set_filter, route_key))
            self._more_menu.addAction(action)
            self._more_actions[route_key] = action
        self.more_filter_btn.setMenu(self._more_menu)
        self.header_layout.addWidget(self.more_filter_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        # === 当前二级筛选的 chip ===
        # `PillPushButton` 本体是 TogglePushButton，这里只做展示 + 一键清除 →
        # 关掉 checkable，未选中态正好是想要的浅灰胶囊。
        self.filter_chip = PillPushButton(FluentIcon.CLOSE, "", self)
        self.filter_chip.setCheckable(False)
        self.filter_chip.setToolTip(self.tr("清除筛选"))
        self.filter_chip.clicked.connect(self._on_filter_chip_clicked)
        self.filter_chip.hide()
        self.header_layout.addWidget(self.filter_chip, 0, Qt.AlignmentFlag.AlignVCenter)

        # 计数以数字形式写进 tab 文字（见 `_refresh_counts`），颜色信号交给圆点徽标：
        # 库里的 manager 会把数字徽标放到目标外面，实测 39×13 的 InfoBadge 在 pivot 上
        # 右侧和顶部都会被裁掉。徽标的 parent 必须等于 target 的 parent —— 定位用的是
        # `target.geometry()`，那是 parent 坐标系里的值。
        self._failed_dot = DotInfoBadge.error(
            self.pivot,
            target=self.pivot.widget("failed"),
            position=PIVOT_DOT_BADGE_POSITION,
        )
        self._failed_dot.hide()
        self._guard_dot = DotInfoBadge.attension(
            self,
            target=self.more_filter_btn,
            position=PIVOT_DOT_BADGE_POSITION,
        )
        self._guard_dot.hide()

        # 放到最后：`setCurrentItem` 会回发 `currentItemChanged` → `_on_pivot_changed`
        # → `_update_filter_chip()`，chip 必须已经存在。
        self.pivot.setCurrentItem("all")

        self.header_layout.addStretch(1)  # 强制左对齐

        # === 并发数控制 ===
        self.concurrent_label = BodyLabel(self.tr("并发下载数:"), self)
        self.header_layout.addWidget(self.concurrent_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.concurrent_box = ComboBox(self)
        self.concurrent_box.addItems([str(i) for i in range(1, 11)])
        # 标签在窄窗口下会被 `_apply_responsive_header()` 收起来，语义得由下拉框自己兜住
        self.concurrent_box.setToolTip(self.tr("并发下载数"))
        # Fetch initial value from config
        current_max = int(config_manager.get("max_concurrent_downloads", 3) or 3)
        self.concurrent_box.setCurrentIndex(max(0, min(9, current_max - 1)))
        self.concurrent_box.setFixedWidth(65)
        self.concurrent_box.setFixedHeight(32)  # 防止被拉伸
        self.concurrent_box.currentIndexChanged.connect(self._on_concurrent_changed)
        self.header_layout.addWidget(self.concurrent_box, 0, Qt.AlignmentFlag.AlignVCenter)

        # 并发数是「设置」、右边是「动作」，两组之间留出比组内更大的间隔（8 + 8 + 8）
        self.header_layout.addSpacing(_HEADER_SPACING)

        # === 排序切换按钮 ===
        from qfluentwidgets import ToolButton

        self.sort_button = ToolButton(FluentIcon.UP, self)
        self.sort_button.setToolTip(self.tr("切换排序 (最新/最早)"))
        self.sort_button.clicked.connect(self._on_sort_clicked)
        self.header_layout.addWidget(self.sort_button, 0, Qt.AlignmentFlag.AlignVCenter)

        # === 动作按钮扩展槽 ===
        self.action_layout = QHBoxLayout()
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.header_layout.addLayout(self.action_layout)

        self.v_layout.addLayout(self.header_layout)

        # SegmentedWidget 自带容器背景，不再需要额外的分割线，或者保留分割线作为区域划分
        # 用户建议: self.tr("去掉下划线...那个蓝绿色的下划线就可以去掉了") -> SegmentedWidget 没有下划线
        # 用户建议: self.tr("下方可以有一条贯穿全宽的细分割线") -> 保留分割线作为区域划分

        # === 分割线 (保留以区分区域) ===
        self.pivot_line = QFrame(self)
        self.pivot_line.setFrameShape(QFrame.Shape.HLine)
        self.pivot_line.setFrameShadow(QFrame.Shadow.Plain)
        self.v_layout.addWidget(self.pivot_line)

        # === 堆叠视图 (StackedWidget) 防止错位 ===
        from PySide6.QtWidgets import QStackedWidget

        self.stack = QStackedWidget(self)
        self.v_layout.addWidget(self.stack, 1)

        # === 任务列表 ListView ===
        # 用 TaskListView 而不是裸 ListView：后者的 _setHoverRow/_setPressedRow/
        # _setSelectedRows 都是全视口重绘，叠加 60fps 悬停动画会变成「加动画 == 加卡顿」。
        from .views.task_list_view import TaskListView

        self.list_view = TaskListView(self)
        # 先挂 delegate 再挂 model：TaskListView.setModel 里会同步动画器状态，
        # 那时 itemDelegate() 应当已经是我们自己的 delegate。
        self.list_view.setItemDelegate(self.delegate)
        self.list_view.setModel(self.proxy_model)
        self.list_view.setFrameShape(QFrame.Shape.NoFrame)
        self.list_view.setStyleSheet("background: transparent;")
        self.list_view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        # 常驻多选。Ctrl / Shift / Ctrl+A / 拖拽框选全部由 Qt 提供，我们一行都不用写；
        # 而且 selectAll() 走的是 proxy，天然只覆盖当前筛选可见的行。
        self.list_view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.list_view.setSpacing(8)
        # 所有行等高（delegate.ITEM_HEIGHT 固定 100px）→ QListView 直接算出行位置，
        # 不再为每一行调用 sizeHint()。两个同族列表早就设了，只有下载列表漏了。
        self.list_view.setUniformItemSizes(True)
        self.list_view.setMouseTracking(True)  # 重要：启用鼠标追踪以支持 Delegate 悬停状态

        self.stack.addWidget(self.list_view)

        # === 空状态占位符 (增强版) ===
        from qfluentwidgets import PrimaryPushButton

        self.empty_placeholder = QWidget(self)
        empty_layout = QVBoxLayout(self.empty_placeholder)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setSpacing(16)

        # 使用更大的图标 (FluentIcon.LIBRARY 或自定义图)
        # 这里模拟插画效果，使用较大的 Icon
        self.empty_icon = QLabel(self.empty_placeholder)
        # 实际项目中应加载 SVG/PNG 插画
        # self.empty_icon.setPixmap(...)
        # 暂时用大号 Emoji 或 Icon 替代
        self.empty_icon.setText("🍃")
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

        text_container = QWidget(self.empty_placeholder)
        text_layout = QVBoxLayout(text_container)
        text_layout.setSpacing(4)

        self.empty_title = SubtitleLabel(self.tr("暂无任务"), text_container)
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.empty_desc = BodyLabel(self.tr("点击下方按钮新建下载任务"), text_container)
        self.empty_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_desc.setTextColor(
            QColor(96, 96, 96), QColor(206, 206, 206)
        )  # Secondary text color

        text_layout.addWidget(self.empty_title)
        text_layout.addWidget(self.empty_desc)

        # 行动点按钮 (!Action)
        self.empty_action_btn = PrimaryPushButton(
            FluentIcon.ADD, self.tr("新建任务"), self.empty_placeholder
        )
        self.empty_action_btn.setFixedWidth(160)
        # 连接到跳转信号
        self.empty_action_btn.clicked.connect(self.route_to_parse.emit)

        empty_layout.addStretch(1)
        empty_layout.addWidget(self.empty_icon)
        empty_layout.addWidget(text_container)
        empty_layout.addWidget(self.empty_action_btn)
        empty_layout.addStretch(1)

        self.stack.addWidget(self.empty_placeholder)
        self.stack.setCurrentWidget(self.list_view)

        # === 悬浮批量操作面板 ===
        from .batch_operation_panel import BatchOperationPanel

        self.batch_panel = BatchOperationPanel(self)

        # 连接面板信号 → 页面信号
        self.batch_panel.select_all_requested.connect(self.select_all)
        self.batch_panel.clear_selection_requested.connect(self.clear_selection)
        self.batch_panel.batch_start_requested.connect(
            lambda: self.batch_start_requested.emit(self.get_selected_rows())
        )
        self.batch_panel.batch_pause_requested.connect(
            lambda: self.batch_pause_requested.emit(self.get_selected_rows())
        )
        self.batch_panel.batch_delete_requested.connect(
            lambda delete_files: self.batch_delete_requested.emit(
                self.get_selected_rows(), delete_files
            )
        )

        # 选中集合变化 → 批量条滑入/滑出。接视图自己的信号而不是
        # `selectionModel().selectionChanged`：`setModel()` 会换掉整个 selection model，
        # 直接接在上面的连接会静默失效。
        self.list_view.selection_changed.connect(self._refresh_batch_panel)
        self.list_view.context_menu_requested.connect(self._on_context_menu)
        # 行数变化会改动计数里的「总数」，筛选切换也会（proxy 重新过滤 → rowsRemoved，
        # 被隐藏的行由 selectionModel 自动剔除，但那条 selectionChanged 不含「总数」信息）。
        self.proxy_model.rowsInserted.connect(self._refresh_batch_panel)
        self.proxy_model.rowsRemoved.connect(self._refresh_batch_panel)
        self.proxy_model.modelReset.connect(self._refresh_batch_panel)

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)
        self._update_style()

        # 响应配制改变，同步并发数数值
        from ..core.config_manager import config_manager

        config_manager.configChanged.connect(self._on_global_config_changed)

        # === 计数刷新的触发源 ===
        # `download_manager.task_updated`（8 处发射、此前零连接）覆盖新增/结束/删除/暂停；
        # 模型的结构信号补上「分页补进历史行」和「清空」这两类它管不到的变化。
        # 接 **source** 而不是 proxy：切筛选会让 proxy 增删行，但总数一个都没变。
        from ..download.download_manager import download_manager

        download_manager.task_updated.connect(self._schedule_counts_refresh)
        self.model.rowsInserted.connect(self._schedule_counts_refresh)
        self.model.rowsRemoved.connect(self._schedule_counts_refresh)
        self.model.modelReset.connect(self._schedule_counts_refresh)
        self._recount()

    def _on_global_config_changed(self, key: str, value: Any) -> None:
        if key == "max_concurrent_downloads":
            try:
                val_int = int(value)
                index_target = max(0, min(9, val_int - 1))
                if self.concurrent_box.currentIndex() != index_target:
                    self.concurrent_box.setCurrentIndex(index_target)
            except (ValueError, TypeError):
                pass

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._schedule_responsive_header()
        # 摆位与滑动动画都归面板自己管，页面只负责通知尺寸变了
        if hasattr(self, "batch_panel"):
            self.batch_panel.relayout()

    def _schedule_responsive_header(self) -> None:
        """把顶栏降级判定推到下一个事件循环回合。

        **不能同步判定**：判定要读 `header_layout.minimumSize()`，而这一行里 pivot 的
        最小宽是**延迟生效**的。`SegmentedWidget.hBoxLayout` 的 sizeConstraint 是
        `SetMinimumSize`，它把「各 tab 最小宽之和」写进 pivot **控件**的 `minimumSize`
        这一步，只发生在那条 posted `LayoutRequest` 被投递到 pivot 的时候；在那之前
        `qSmartMinSize()` 会用仍是旧值的 `minimumSize` 把新的 `minimumSizeHint` 压回去。
        实测：`setItemText` 之后立刻读，`pivot.minimumSizeHint()` 已经是 516 了，但
        `header_layout.itemAt(0).minimumSize()` 还是 348，整行最小宽因此少算 168px ——
        `invalidate()` 和 `activate()` 都救不了，只有事件投递能。

        所以触发点只负责「记一笔」，判定统一在事件排空后做。零延时单次定时器足够：
        posted 事件在同一轮里先于定时器派发。
        """
        if not hasattr(self, "_header_timer"):
            return
        if not self._header_timer.isActive():
            self._header_timer.start()

    def _apply_responsive_header(self) -> None:
        """行内实在摆不下时，把「并发下载数:」这个纯说明性标签收起来。

        顶栏的降级顺序是：先由 `ResponsiveCommandBar` 把动作收进「更多」菜单（它自己的
        `resizeEvent` 就会做），全折完了还差就轮到这里。之所以还需要这一步：4 个 tab
        同时到 `999+` 时 pivot 的**硬**最小宽是 516px（`PivotItem.minimumSizeHint()`
        按文字算，和我们预留的宽度无关），叠上 chip、并发数、排序按钮和已经只剩 40px 的
        CommandBar，在窗口最小宽 + 侧边栏展开（占 142px）下仍会超出约 44px ——
        超出的那一刻 `QHBoxLayout` 只能去压别人，压出来就是错叠。收起标签让出 92px
        （84 + 一个间距），整行最小宽回到 871，比可用宽 919 还剩 48px 余量。

        标签的语义不会丢：下拉框自己带同样文案的 tooltip。

        两个阈值构成滞回，避免在临界点上反复显隐：藏是「现在就摆不下」，恢复是
        「把标签连同它的间距一起加回来也还摆得下」。

        折叠状态记在 `_header_label_collapsed` 而不是回读 `isVisible()`：页面在
        `QStackedWidget` 里不是当前页时，所有子控件的 `isVisible()` 都是 False，
        用它当状态位会让「该藏的」永远走进恢复分支。

        直接调用它要求布局已经稳定，正常路径请走 `_schedule_responsive_header()`。
        """
        if self._adjusting_header or not hasattr(self, "concurrent_label"):
            return
        available = self.width() - 2 * _PAGE_MARGIN
        if available <= 0:  # 还没有真实几何（首次 show 之前）
            return
        self._adjusting_header = True
        try:
            needed = self.header_layout.minimumSize().width()
            if not self._header_label_collapsed:
                if needed > available:
                    self.concurrent_label.hide()
                    self._header_label_collapsed = True
            else:
                cost = self.concurrent_label.sizeHint().width() + _HEADER_SPACING
                if needed + cost <= available:
                    self.concurrent_label.show()
                    self._header_label_collapsed = False
        finally:
            self._adjusting_header = False

    def _update_style(self):
        from qfluentwidgets import isDarkTheme

        line_color = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.08)"
        self.pivot_line.setStyleSheet(f"color: {line_color};")

        empty_color = "rgba(255, 255, 255, 0.1)" if isDarkTheme() else "rgba(0, 0, 0, 0.1)"
        self.empty_icon.setStyleSheet(f"font-size: 64px; color: {empty_color};")

    # === 多选与批量作用域 ===

    def selected_count(self) -> int:
        """当前选中行数。

        用 `QItemSelectionRange.height()` 求和而不是 `len(selectedRows())`：拖拽框选时
        每一次鼠标移动都会发一次 selectionChanged，物化整个选中集合等于每帧新建成千上万个
        QModelIndex。区间数量通常是个位数。
        """
        selection_model = self.list_view.selectionModel()
        if selection_model is None:
            return 0
        return sum(rng.height() for rng in selection_model.selection())

    def _selected_proxy_rows(self) -> list[int]:
        """选中的 proxy 行号，升序去重。只在真正要执行动作时调用。

        Rows 模式下 Qt 给出的区间实际不会重叠，但它并不保证已经合并过，
        所以仍然过一遍 set —— 否则重复行会让批量删除对同一行执行两次。
        """
        selection_model = self.list_view.selectionModel()
        if selection_model is None:
            return []
        rows: set[int] = set()
        for rng in selection_model.selection():
            rows.update(range(rng.top(), rng.bottom() + 1))
        return sorted(rows)

    def get_selected_rows(self) -> list[int]:
        """选中行映射为 **source** 行号 —— MainWindow 侧的批量槽签名因此保持不变。"""
        rows: list[int] = []
        for proxy_row in self._selected_proxy_rows():
            src_idx = self.proxy_model.mapToSource(self.proxy_model.index(proxy_row, 0))
            if src_idx.isValid():
                rows.append(src_idx.row())
        return rows

    def select_all(self) -> None:
        """全选，**只作用于当前筛选可见的行**。

        `QListView.selectAll()` 作用在 proxy 上，天然只覆盖可见行。旧实现调
        `model.set_all_selected(True)` 直接打到 source 全表，于是在「已完成」页签下点全选
        会连带选上正在下载的任务。
        """
        self.list_view.selectAll()

    def clear_selection(self) -> None:
        self.list_view.clearSelection()

    def _refresh_batch_panel(self, *args) -> None:
        """按选中数决定批量条的可见性与文案。"""
        if not hasattr(self, "batch_panel"):
            return
        selected = self.selected_count()
        if selected == 0 and not self.batch_panel.isVisible():
            # 常态：没有选中项，面板也没露出来。批量插入行时会高频走到这里，直接返回。
            return
        # 「总数」是当前筛选可见数而不是全表 —— 面板上的动作也只作用于这个范围。
        self.batch_panel.update_count(selected, self.proxy_model.rowCount())
        if selected > 0:
            self.batch_panel.slide_in()
        else:
            self.batch_panel.slide_out()

    def current_filter_label(self) -> str:
        """当前筛选的显示名，供批量确认框写明作用域。

        走自己维护的 `_filter_labels` 而不是 `SegmentedWidget.widget(routeKey)` ——
        后者在 routeKey 不存在时会抛。
        """
        return self._filter_labels.get(self._current_filter, self._filter_labels["all"])

    # === 右键菜单 ===

    def _row_at(self, proxy_row: int) -> TaskRow | None:
        """proxy 行号 → 行对象。历史行也返回（它没有 worker，但有完整快照）。"""
        src_idx = self.proxy_model.mapToSource(self.proxy_model.index(proxy_row, 0))
        if not src_idx.isValid():
            return None
        return self.model.get_task(src_idx.row())

    def _menu_scope(self, proxy_row: int) -> tuple[list[int], Callable[[], list[int]]] | None:
        """右键菜单作用于哪些行，以及**在点下去的那一刻**怎么重新求出它们。

        右键不再改选中集合（`TaskListView` 关掉了 `setSelectRightClickedRow`），作用域
        因此要在这里判：

        * 右键落在**已勾选**的行上、或落在空白处 → 作用于整批勾选。
          「勾了 5 个，右键其中一个 → 对 5 个都生效」。
        * 右键落在**未勾选**的行上 → **只**作用于那一行，勾选集合一根汗毛都不动。
          旧行为是把它改成独占选中，于是已经勾好的 N 个当场消失，复选框还会在每一行冒出来、
          批量条跟着滑进来 —— 右键和勾选同时发生。这正是「勾选就勾选取消就取消」要禁掉的。

        返回的第二项是**延迟求值**的，不是行号快照：`RoundMenu.exec()` 不阻塞，菜单挂着的
        这段时间里 `add_task` 会往第 0 行插新任务，把所有行号往下推。两种作用域各有各的
        抗平移办法 —— 整批走 `get_selected_rows()`（Qt 自己会平移 selection），单行走
        source 模型上的 `QPersistentModelIndex`（Qt 同样会替它平移）。
        """
        selection_model = self.list_view.selectionModel()
        index = self.proxy_model.index(proxy_row, 0) if proxy_row >= 0 else QModelIndex()
        # 问 selectionModel 而不是拿 `_selected_proxy_rows()` 去 in：勾了上万行时不必
        # 为了一次判断把整个集合物化出来。
        on_selected = (
            selection_model is not None and index.isValid() and selection_model.isSelected(index)
        )

        if proxy_row < 0 or on_selected:
            rows = self._selected_proxy_rows()
            # 空白处 + 一个都没勾 —— 没有作用域可言
            return (rows, self.get_selected_rows) if rows else None

        src_idx = self.proxy_model.mapToSource(index)
        if not src_idx.isValid():
            return None
        # 存 **source** 的持久索引：`.row()` 直接就是批量槽要的 source 行号，不必再过一次
        # proxy 映射（proxy 行号还会随筛选变化而失效）。行被删掉时它自己变 invalid，
        # resolve 返回空列表，各批量槽都有「空集合直接 return」的守卫。
        anchor = QPersistentModelIndex(src_idx)
        return [proxy_row], lambda: [anchor.row()] if anchor.isValid() else []

    def _emit_folder(self, source_rows: list[int]) -> None:
        """打开所在文件夹。只在作用域正好是一行时有意义（菜单项本身也只在单行时可用）。"""
        if len(source_rows) == 1:
            self.card_folder_requested.emit(source_rows[0])

    def _on_context_menu(self, proxy_row: int, global_pos: QPoint) -> None:
        """按作用域内各行的状态构建右键菜单。作用域判定见 `_menu_scope`。"""
        scope = self._menu_scope(proxy_row)
        if scope is None:
            return
        rows, resolve = scope

        # 历史行同样进作用域：`url` / `effective_state` 都从快照读得到，
        # 所以「复制链接」「重新解析」「删除」对没有 worker 的行照样可用。
        row_objs = [r for r in (self._row_at(row) for row in rows) if r is not None]
        states = {r.effective_state for r in row_objs}
        urls = [url for url in (r.url for r in row_objs) if url]
        single = len(rows) == 1

        menu = RoundMenu(parent=self)

        # 整个作用域都是终态失败/取消时，「开始」其实是**重下**，叫「重试」更准。
        # 混选（有暂停也有失败）时保持「开始」—— 那是它对多数行的真实语义。
        if states and states <= _RETRY_STATES:
            act_start = Action(FluentIcon.UPDATE, self.tr("重试"), self)
        else:
            act_start = Action(FluentIcon.PLAY, self.tr("开始"), self)
        act_start.setEnabled(bool(states & _STARTABLE_STATES))
        act_start.triggered.connect(lambda: self.batch_start_requested.emit(resolve()))
        menu.addAction(act_start)

        act_downgrade = Action(FluentIcon.CARE_DOWN_SOLID, self.tr("降低画质重试"), self)
        # 只按状态开放。「这一行到底是不是严格档位任务」要读 `ydl_opts_json`，
        # 建菜单时按行查库会把右键点击拖慢（选中上千行时更明显）—— 那个判断留到
        # 真正点下去之后，不是严格档位的行由 MainWindow 汇总成一条提示。
        act_downgrade.setEnabled(bool(states & _DOWNGRADABLE_STATES))
        act_downgrade.triggered.connect(lambda: self.batch_downgrade_requested.emit(resolve()))
        menu.addAction(act_downgrade)

        act_pause = Action(FluentIcon.PAUSE, self.tr("暂停"), self)
        act_pause.setEnabled(bool(states & _PAUSABLE_STATES))
        act_pause.triggered.connect(lambda: self.batch_pause_requested.emit(resolve()))
        menu.addAction(act_pause)

        menu.addSeparator()

        act_folder = Action(FluentIcon.FOLDER, self.tr("打开所在文件夹"), self)
        # MainWindow 那侧是按单行打开的，多选时会一次弹出一堆资源管理器窗口，只在单选时开放
        act_folder.setEnabled(single)
        act_folder.triggered.connect(lambda: self._emit_folder(resolve()))
        menu.addAction(act_folder)

        act_copy = Action(FluentIcon.COPY, self.tr("复制链接"), self)
        act_copy.setEnabled(bool(urls))
        # url 是字符串快照，不随行号平移，可以放心在建菜单时就取好
        act_copy.triggered.connect(lambda: self._copy_urls(urls))
        menu.addAction(act_copy)

        act_reparse = Action(FluentIcon.SYNC, self.tr("重新解析"), self)
        # 重新解析要拉起解析对话框，一次只能处理一个 url
        act_reparse.setEnabled(single and bool(urls))
        act_reparse.triggered.connect(lambda: self.reparse_requested.emit(urls[0]))
        menu.addAction(act_reparse)

        menu.addSeparator()

        act_delete = Action(FluentIcon.DELETE, self.tr("删除记录"), self)
        act_delete.triggered.connect(lambda: self.batch_delete_requested.emit(resolve(), False))
        menu.addAction(act_delete)

        act_delete_files = Action(FluentIcon.DELETE, self.tr("连同文件一起删除"), self)
        act_delete_files.triggered.connect(
            lambda: self.batch_delete_requested.emit(resolve(), True)
        )
        menu.addAction(act_delete_files)

        # 回收上一次的菜单。`RoundMenu.exec()` 是**非阻塞**的（内部只是 show()），
        # 所以不能在本函数末尾删；也不能在自己的 closedSignal 里删自己 ——
        # 菜单是先 close() 再 action.trigger() 的，而 trigger 可能打开模态对话框，
        # 那个嵌套事件循环会正好执行到排队中的 deleteLater。改成建新的时候删旧的，
        # 任何时刻最多存在两个。
        old_menu = getattr(self, "_context_menu", None)
        if old_menu is not None:
            old_menu.deleteLater()
        self._context_menu = menu
        # 给被右键的那一行一个视觉焦点。右键不再选中它，而 `RoundMenu` 是个抢鼠标的 popup
        # ——菜单一弹出视图就收到 Leave，悬停高亮当场消失，于是这一行会**毫无标记**，
        # 而菜单里摆着「连同文件一起删除」。作用域是整批时靠复选框本身就看得出来，
        # 所以只在右键确实落在某一行上时点亮它。
        self.list_view.set_context_row(proxy_row)
        menu.closedSignal.connect(lambda: self.list_view.set_context_row(-1))
        menu.exec(global_pos)

    def _copy_urls(self, urls: list[str]) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None or not urls:
            return
        clipboard.setText("\n".join(urls))
        InfoBar.success(
            title=self.tr("已复制"),
            content=self.tr("{} 个链接已复制到剪贴板").format(len(urls)),
            duration=2000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _reserve_pivot_widths(self, texts: dict[str, str] | None = None) -> None:
        """给每个主 tab 占住「它到目前为止显示过的最长文案」的宽度。

        计数是写进 tab 文字里的（见 `_refresh_counts`），文字变长 tab 就会跟着变宽；而
        `SegmentedWidget` 的胶囊位置来自 `slideAni`，它只在 `setCurrentItem` /
        `showEvent` / `resizeEvent` 里重新同步。两个 tab 一个变宽一个变窄时 pivot 总宽
        不变 → 没有 resizeEvent → 胶囊停在旧坐标上。占住宽度就不会「一个宽一个窄」。

        **只涨不落**（`_pivot_reserved` 是历史最大值）：这才是「不抖」的充分条件，
        而按 `999+` 一次性占满不是必要条件 —— 后者在真实计数下凭空多要约 168px
        （实测 4 个 tab 全按 `999+` 预留时 pivot 最小宽 516px，按真实计数只要 348px），
        再叠上 `SegmentedWidget` 那个硬最小宽（`hBoxLayout` 是 `SetMinimumSize`，
        `setMinimumWidth` 会一路传导成 pivot 自己的 `minimumWidth`），在 1150px
        的窗口最小宽下整行就摆不下了：`QHBoxLayout` 把 pivot 的格子压到最小宽以下，
        `QWidget.setGeometry` 又把控件反弹回最小宽，pivot 于是画到邻居身上
        —— 这就是「更多筛选」按钮被压在 tab 底下的成因。

        要说清这里的**边界**：`PivotItem.minimumSizeHint()` 是按当前文字算的，和我们
        `setMinimumWidth` 预留多少无关。所以计数真的涨到 `999+` 时，把预留全清掉也救不了
        整行 —— 那时 pivot 的 516px 是文字挣来的硬下限，得靠
        `_apply_responsive_header()` 从行内别处让出宽度。预留的作用**只是防抖**，
        不是这条硬下限的来源。

        `+24` 对齐 PivotItem 自带的 `padding: 0 12px`。
        """
        grew = False
        for route_key in _MAIN_BUCKETS:
            item = self.pivot.widget(route_key)
            text = (texts or {}).get(route_key) or self._filter_labels[route_key]
            reserved = item.fontMetrics().horizontalAdvance(text) + 24
            if reserved > self._pivot_reserved.get(route_key, 0):
                self._pivot_reserved[route_key] = reserved
                item.setMinimumWidth(reserved)
                grew = True
        if grew:
            self._resync_pivot_indicator()

    def _resync_pivot_indicator(self) -> None:
        """让胶囊重新贴合当前 tab。

        `setItemText` 不会重新同步 `slideAni`，`setCurrentItem` 遇到同一个 routeKey 又会
        提前 return，所以改完文字/宽度必须自己补一刀。`setIndicatorLength()` 是库里
        唯一公开的 `_adjustIndicatorPos()` 入口（赋值 + 立即重新定位），拿当前值回填
        就是一次「无副作用的重新同步」，不用碰库的私有方法。
        """
        self.pivot.setIndicatorLength(self.pivot.indicatorLength())

    def _on_pivot_changed(self, route_key: str) -> None:
        """Pivot 切换时调用"""
        if self._syncing_pivot:
            # `set_filter` 正在把 pivot 拨回「全部」（二级筛选生效时）。这条回声若不拦，
            # 就会用桶名把刚设好的二级筛选覆盖掉。
            return
        self._apply_filter(route_key)
        self._update_filter_chip()

    def _on_concurrent_changed(self, index: int) -> None:
        """并发数改变时更新配置并通知管理器"""
        from ..core.config_manager import config_manager
        from ..download.download_manager import download_manager

        value = index + 1
        config_manager.set("max_concurrent_downloads", value)
        # Force manager to evaluate queued items based on new limit
        download_manager.pump()

    def _on_sort_clicked(self):
        from PySide6.QtCore import Qt
        from qfluentwidgets import FluentIcon

        if self.proxy_model.sortOrder() == Qt.SortOrder.AscendingOrder:
            self.proxy_model.sort(0, Qt.SortOrder.DescendingOrder)
            self.sort_button.setIcon(FluentIcon.DOWN)
        else:
            self.proxy_model.sort(0, Qt.SortOrder.AscendingOrder)
            self.sort_button.setIcon(FluentIcon.UP)

    def set_filter(self, status: str) -> None:
        """设置过滤条件。主 pivot 的桶名与「更多筛选」里的二级筛选走同一个入口。"""
        if status not in _FILTER_STATES:
            # 未知桶名退回「全部」：宁可「筛选没生效」，也不要「列表整个空掉」。
            status = "all"
        # 二级筛选没有自己的 tab，pivot 停在「全部」，靠右侧的 chip 说明当前作用域。
        self._sync_pivot(status if status in _MAIN_BUCKETS else "all")
        self._apply_filter(status)
        self._update_filter_chip()
        if status == "missing":
            # 「文件丢失」是唯一需要现场数据的筛选：`file_exists` 默认是 None
            # （= 按存在渲染），不重扫一遍就永远筛不出东西。结论由 `FileExistenceProbe`
            # 流式回来，列表会渐进填充 —— 所以这里不阻塞，也不等它。
            self.model.rescan_existence()

    def _sync_pivot(self, route_key: str) -> None:
        """把 pivot 拨到某一项，且不让它的回声反过来改筛选。"""
        self._syncing_pivot = True
        try:
            self.pivot.setCurrentItem(route_key)
        finally:
            self._syncing_pivot = False

    def _apply_filter(self, status: str) -> None:
        """真正落地筛选（不动 pivot，避免和它的信号互相触发）。"""
        self._current_filter = status
        self.proxy_model.set_filter(status)
        self._update_empty_state()
        # 被筛掉的行由 selectionModel 自动剔除（proxy 重新过滤会发 rowsRemoved），
        # 但「一行都没变」时那条信号不会来，而「总数」照样变了 —— 显式刷一次，计数不会漂。
        self._refresh_batch_panel()

    def _update_filter_chip(self) -> None:
        """二级筛选生效时显示可关闭的 chip；主桶不需要（pivot 自己就是指示）。"""
        if self._current_filter in _MAIN_BUCKETS:
            self.filter_chip.hide()
        else:
            self.filter_chip.setText(self._filter_labels.get(self._current_filter, ""))
            self.filter_chip.show()
        # chip 是这一行最大的一次性增量（约 112px 含间距），显隐后必须重新判定降级
        self._schedule_responsive_header()

    def _on_filter_chip_clicked(self) -> None:
        self.set_filter("all")

    # === 计数 ===

    def bucket_counts(self) -> dict[str, int]:
        """各桶 / 二级筛选的任务数。两个来源的分工依据见模块顶部
        `_MEMORY_COUNT_STATES` 的注释：终态问 DB，非终态问模型，**绝不相加**。"""
        from ..storage.task_db import task_db

        mem = self.model.get_counts_by_state()
        try:
            db_counts = task_db.count_tasks_by_state()
        except Exception:
            # 计数只是装饰，数不出来就当 0，绝不能让它把页面打挂。
            db_counts = {}

        return {
            "all": _sum_states(db_counts, _DB_COUNT_STATES)
            + _sum_states(mem, _MEMORY_COUNT_STATES),
            "active": _sum_states(mem, _FILTER_STATES["active"]),
            "completed": _sum_states(db_counts, _FILTER_STATES["completed"]),
            "failed": _sum_states(db_counts, _FILTER_STATES["failed"]),
            "queued": _sum_states(mem, _FILTER_STATES["queued"]),
            "paused": _sum_states(mem, _FILTER_STATES["paused"]),
            "quality_guard": _sum_states(mem, _FILTER_STATES["quality_guard"]),
            "cancelled": _sum_states(db_counts, _FILTER_STATES["cancelled"]),
            # 「文件丢失」数不出来，见 `_refresh_counts`
            "missing": 0,
        }

    @staticmethod
    def _format_count(n: int) -> str:
        return f"{n}" if n <= _COUNT_DISPLAY_MAX else f"{_COUNT_DISPLAY_MAX}+"

    def _schedule_counts_refresh(self, *args) -> None:
        """请求一次计数刷新。`task_updated` 在下载过程中是高频信号，必须合并。"""
        if not self._counts_timer.isActive():
            self._counts_timer.start()

    def _refresh_counts(self) -> None:
        texts = {}
        for route_key in _MAIN_BUCKETS:
            label = self._filter_labels[route_key]
            n = self._counts.get(route_key, 0)
            texts[route_key] = label if n <= 0 else f"{label} {self._format_count(n)}"
        # 只在文案真的变了时才动 pivot：这条每 300ms 就会跑一次，而 `setItemText` →
        # `setText` 会让 tab 重新算尺寸、跟着重排整行。顺带也把胶囊的重新同步
        # （会打断切换动画）限制在「计数确实跳了」的那一帧。
        changed = {k: v for k, v in texts.items() if self.pivot.widget(k).text() != v}
        if changed:
            for route_key, text in changed.items():
                self.pivot.setItemText(route_key, text)
            self._reserve_pivot_widths(texts)
            self._resync_pivot_indicator()
            # 计数变长会把 pivot 的最小宽推高，可能就此越过临界点
            self._schedule_responsive_header()
        for route_key, action in self._more_actions.items():
            label = self._filter_labels[route_key]
            if route_key == "missing":
                # 「文件丢失」数不出来：DB 不知道文件在不在，模型里也只有分页读进来的
                # 前缀。写一个偏小的数字比不写更糟 —— 用户会以为只丢了那几个。
                action.setText(label)
                continue
            n = self._counts.get(route_key, 0)
            action.setText(label if n <= 0 else f"{label} ({self._format_count(n)})")
        # 颜色信号交给圆点徽标：数字已经在 tab 文字里，再挂一个数字徽标只会被裁掉。
        self._set_dot_visible(self._failed_dot, self._counts.get("failed", 0) > 0)
        # 质量守卫被降级进下拉，圆点是它唯一的「我这儿有东西」信号。
        self._set_dot_visible(self._guard_dot, self._counts.get("quality_guard", 0) > 0)

    def _recount(self) -> None:
        """重新统计并刷新所有计数展示。由 300ms 合并窗口触发。"""
        self._counts = self.bucket_counts()
        self._refresh_counts()
        self.counts_changed.emit(dict(self._counts))

    @staticmethod
    def _set_dot_visible(badge, visible: bool) -> None:
        if not visible:
            badge.hide()
            return
        # 目标控件可能在首次 show 之后才拿到最终几何（pivot 撑宽、chip 显隐都会推动它），
        # 所以每次显示都重新问一遍 manager 要坐标。
        manager = getattr(badge, "manager", None)
        if manager is not None:
            badge.move(manager.position())
        badge.show()
        badge.raise_()

    def _update_empty_state(self, *args) -> None:
        """请求一次空状态刷新（合并到单个去抖定时器，模型更新过程中不立即执行）"""
        self._empty_state_timer.start()

    def _apply_empty_state(self) -> None:
        """真正切换空状态占位符与列表视图"""
        if not hasattr(self, "proxy_model") or not hasattr(self, "stack"):
            return

        visible_count = self.proxy_model.rowCount()

        if visible_count == 0:
            self.stack.setCurrentWidget(self.empty_placeholder)

            # 根据当前过滤器显示不同文案。键必须覆盖 `_FILTER_STATES` 的全部桶名，
            # 否则落到兜底的「🍃 暂无任务」，用户读不出「是筛掉了还是真没有」。
            messages = {
                "all": ("🍃", self.tr("暂无任务"), self.tr("点击「新建任务」开始下载")),
                "active": (
                    "⏳",
                    self.tr("没有进行中的任务"),
                    self.tr("下载中、排队和已暂停的任务会显示在这里"),
                ),
                "completed": (
                    "✅",
                    self.tr("没有已完成的任务"),
                    self.tr("完成的任务会显示在这里"),
                ),
                "failed": ("❌", self.tr("没有失败的任务"), self.tr("太棒了，一切顺利！")),
                "queued": ("📋", self.tr("没有排队中的任务"), self.tr("所有任务已开始")),
                "paused": ("⏸️", self.tr("没有暂停的任务"), self.tr("所有任务运行中")),
                "quality_guard": (
                    "🛡️",
                    self.tr("没有被质量守卫拦下的任务"),
                    self.tr("画质都符合预期"),
                ),
                "cancelled": ("🚫", self.tr("没有已取消的任务"), self.tr("没有中途放弃的下载")),
                # 存在性检查是流式回来的，所以这里不能写死「都还在」的结论
                "missing": (
                    "🔍",
                    self.tr("没有发现丢失的文件"),
                    self.tr("正在后台核对已完成的任务，有结果会自动出现在这里"),
                ),
            }
            icon, title, subtitle = messages.get(
                self._current_filter, ("🍃", self.tr("暂无任务"), "")
            )
            self.empty_icon.setText(icon)
            self.empty_title.setText(title)
            self.empty_desc.setText(subtitle)

            # 仅在 'all' 过滤器下显示行动按钮
            self.empty_action_btn.setVisible(self._current_filter == "all")
        else:
            self.stack.setCurrentWidget(self.list_view)

    def count(self) -> int:
        """返回卡片总数"""
        return self.model.rowCount()

    def visible_count(self) -> int:
        """返回当前可见卡片数"""
        return self.proxy_model.rowCount()

    def get_counts_by_state(self) -> dict[str, int]:
        """获取各状态的任务计数"""
        return self.model.get_counts_by_state()

    # === 行操作的公开接口 ===
    #
    # MainWindow 原来直接伸手进 `task_page.model` / `task_page.proxy_model`（十几处），
    # 于是「换掉行结构」这种纯页面内部的改动会同时打穿主窗口。对外只暴露下面这几个方法：
    # 行号一律是 **source** 行号（批量槽的既有签名就是 source 行号，不改）。

    def add_task(self, worker, title: str = "", thumbnail: str = "") -> None:
        """登记一个活任务。同 `db_id` 的历史行会被就地升级，不会多出一行。"""
        self.model.add_task(worker, title, thumbnail)

    def task_row(self, source_row: int) -> TaskRow | None:
        """source 行号 → 行对象。越界或不存在返回 None。"""
        return self.model.get_task(source_row)

    def task_rows(self, source_rows: list[int]) -> list[TaskRow]:
        """一批 source 行号 → 行对象列表，解析不出来的行直接丢掉。

        融合后不能再只挑「有 worker 的行」：历史行同样要能被删除 / 清空 / 重下。
        取舍留给各个 handler，这里只负责把行号解析成对象。
        """
        out: list[TaskRow] = []
        for row in source_rows:
            row_obj = self.model.get_task(row)
            if isinstance(row_obj, TaskRow):
                out.append(row_obj)
        return out

    def remove_row(self, source_row: int) -> None:
        self.model.remove_task(source_row)

    def remove_row_objects(self, row_objs: list[TaskRow]) -> None:
        """按对象身份把这些行从模型里摘掉。

        行号必须**事后反查**：调用方通常是分块执行的收尾回调，这期间新任务可能插到
        row 0，事前记下的行号会整体平移。反查用 `id(row_obj)`，而调用方的列表在整个
        过程中一直持有强引用，所以 id 不会被回收复用。

        **从下往上删**：先删小行号会让后面的行号全部前移。
        """
        if not row_objs:
            return

        target_ids = {id(r) for r in row_objs}
        for row in range(self.model.rowCount() - 1, -1, -1):
            if id(self.model.get_task(row)) in target_ids:
                try:
                    self.model.remove_task(row)
                except Exception:
                    logger.exception(f"从模型移除第 {row} 行失败")

    def rebind_worker(self, source_row: int, worker) -> None:
        """替换某一行的 worker 并重绑信号。

        已结束的 QThread 不能重启，controller 会用 `restore_db_id` **重建** worker，
        所以「重试 / 继续」之后这一行必须换绑，否则永远停在旧对象的终态上。
        """
        self.model.rebind_worker(source_row, worker)

    def visible_source_rows(self) -> list[int]:
        """当前筛选可见的全部行，映射为 source 行号。

        全局动作（全部开始 / 全部暂停 / 两个清空）一律以此为作用域 —— 看到的和操作的
        必须是同一批东西。
        """
        rows: list[int] = []
        for row in range(self.proxy_model.rowCount()):
            src_idx = self.proxy_model.mapToSource(self.proxy_model.index(row, 0))
            if src_idx.isValid():
                rows.append(src_idx.row())
        return rows
