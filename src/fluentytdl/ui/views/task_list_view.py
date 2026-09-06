"""TaskListView —— 把 qfluentwidgets `ListBase` 的全视口重绘改成定向重绘。

为什么必须有这一层
------------------
`qfluentwidgets/components/widgets/list_view.py` 的 `ListBase` 有三处 `viewport().update()`
是无条件全屏刷新：

- `_setHoverRow`：`entered` 一触发就整屏重绘。今天跨行悬停已经是整屏重绘了，
  一旦叠加 60fps 悬停动画就会变成「加动画 == 加卡顿」。
- `_setPressedRow` / `_setSelectedRows`：当前因 `NoSelection` 提前 return 而从未暴露，
  但阶段 2 启用 `ExtendedSelection` 后每次点击都会整屏重绘。

另外 `_setSelectedRows` 会直接调 `self.delegate.setSelectedRows(indexes)`，而
`setItemDelegate()` 覆写过的自定义 delegate 若没有这个方法，启用多选后第一次点击就
`AttributeError`。所以「常驻多选」和「加动画」这两个决策共享同一个前置条件，就是这个类。

`ListBase.__init__` 用 `self.entered.connect(lambda i: self._setHoverRow(i.row()))`
绑定的是**实例方法**，所以子类覆写天然生效，不需要 disconnect。
`resizeEvent` 的全量 update 保留 —— 它低频且正确。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtWidgets import QApplication
from qfluentwidgets import ListView, ToolTip

# PySide6 没有导出 Qt 的 `QWIDGETSIZE_MAX`，而「把上一次 setFixedSize 留下的约束清掉」
# 只能靠它 —— 不清就会量到上一条文案的宽度（见 _set_tooltip_text）。
_QWIDGETSIZE_MAX = 16777215
# 试排折行时给的高度上限，够高就行，只是为了拿回排版后的宽度。
_WRAP_PROBE_HEIGHT = 1 << 16


class TaskListView(ListView):
    """下载/历史统一任务列表的视图层。"""

    # 右键请求菜单。参数是「被右键的 proxy 行号」（空白处为 -1）和全局坐标。
    # 传行号而不是 QModelIndex：菜单是异步弹出的，QModelIndex 只在当前事件内有效。
    context_menu_requested = Signal(int, QPoint)

    # 选中集合发生变化。页面不要直接接 `selectionModel().selectionChanged` ——
    # `setModel()` 会换掉整个 selection model，接在上面的连接会静默失效。
    selection_changed = Signal()

    # 滚轮动画时长。与 SmoothScrollBar 的默认值一致 —— 历史页的 SmoothScrollArea 用的
    # 就是这个默认值，对齐它才谈得上「和历史页一样的手感」。
    SCROLL_DURATION_MS = 500

    # 单次选中变化超过这么多行就退化成整屏重绘。逐行 visualRect 在 Ctrl+A 这种
    # 一次改上万行的场景下反而比一次全屏更贵。
    BULK_REPAINT_THRESHOLD = 64

    # tooltip 自动隐藏时长。也有一整套事件驱动的隐藏（离开视口/滚动/按下/换行），
    # 计时器是它们全都漏掉时的最后一道 —— `ToolTip` 是 `WindowStaysOnTopHint` 的
    # 顶层窗口，卡住不消失比早消失难受得多。
    TOOLTIP_DURATION_MS = 4000

    # tooltip 宽度上限。标题动辄上百字，`ToolTip` 的 label 又默认不折行 —— 实测 98 个
    # 汉字排成 1176px 宽的一条，比 1024 宽的屏幕还长，右半截直接跑出屏幕外。
    TOOLTIP_MAX_WIDTH = 480

    def __init__(self, parent=None) -> None:
        # ListBase.__init__ 会调用 setItemDelegate()，而我们覆写了它 —— 那里用 getattr
        # 兜底读这些属性，因为 PySide 不允许在 super().__init__() 之前赋值实例属性。
        super().__init__(parent)

        self._hover_row: int = -1
        self._pressed_row: int = -1
        # 这一次按下是不是落在卡片里的复选框/按钮上。松开时要靠它把「点控件」和
        # 「点卡片」分开 —— 见 selectionCommand。
        self._press_on_control: bool = False
        # 按下的视口坐标。松开时靠它和阈值比较，把「点击」和「框选拖拽」分开
        # —— 见 _is_drag_gesture 为什么不能用 state()。
        self._press_pos: QPoint | None = None
        # 「当前是否存在选中项」。复选框的可见性依赖它，所以这个布尔一翻转就意味着
        # **每一行**的画面都要变 —— 那时只能整屏重绘。
        # 只缓存这个布尔、不缓存行集合：布尔用来检测翻转，行集合的真值在 selectionModel。
        self._has_selection: bool = False
        # 被右键的那一行。**只是视觉焦点，不是选中**（见 setSelectRightClickedRow 那段）。
        self._context_row: int = -1
        # Fluent 气泡提示。懒建 —— 构造时 self.window() 还是页面自己（页面这会儿
        # 还没被 addSubInterface 挂进 FluentWindow）。
        self._tooltip: ToolTip | None = None
        self._tooltip_text: str = ""
        # 气泡窗口比里面的文字标签宽/高多少。建气泡时量一次，不照抄上游的边距数字。
        self._tooltip_chrome: int = 0
        self._animator = None
        self._wire_animator(self.itemDelegate())
        self._install_smooth_wheel()
        # 滚动时行会从鼠标底下跑掉，气泡的锚点当场失效
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        # **右键绝不碰选中集合。** `ListView.setSelectRightClickedRow(True)` 会让
        # `ListBase.mousePressEvent` 把右键也交给 `QListView.mousePressEvent`，于是 Qt 的
        # `extendedSelectionCommand` 对未选中行返回 `ClearAndSelect` —— 右键一张卡片就等于
        # 勾选了它：复选框在每一行冒出来、批量条滑进来，和右键菜单同时发生；更糟的是已经勾了
        # N 个时右键第 N+1 张卡会把那 N 个全清掉。这直接违反「勾选就勾选取消就取消」。
        # 显式写 False（也是基类默认值）是为了钉住这个语义 —— 右键的作用域改由
        # `contextMenuEvent` → 页面的 `_menu_scope()` 判定，视觉反馈走 `_context_row`。
        self.setSelectRightClickedRow(False)

    # === 滚轮手感 ===

    def _install_smooth_wheel(self) -> None:
        """把滚轮从 `SmoothScroll` 定时器方案换成 `SmoothScrollBar` 的动画方案。

        两条路径都由 `SmoothScrollDelegate` 提供，差别在构造参数 `useAni`：

        - 历史页 `SmoothScrollArea` 传的是 `useAni=True` → 一格滚轮 = 120px，
          交给 `QPropertyAnimation` 走 500ms `OutCubic`，收尾有减速拖尾。
        - `ListBase` 传的是默认的 `useAni=False` → 走 `SmoothScroll.wheelEvent`，
          其中 `stepRatio=1.5` 且 `acceleration=1`，一格滚轮实际位移 180~360px
          （连续滚动时取上限），而 `LINEAR` 模式的速度曲线是对称三角形，没有拖尾。

        所以同样一格滚轮，任务列表要比历史页多走 1.5~3 倍距离且没有减速段 ——
        这就是「下拉太快、没有动画感」的全部来源，与虚拟化无关。
        """
        delegate = getattr(self, "scrollDelegate", None)
        if delegate is None:
            return

        delegate.useAni = True
        delegate.vScrollBar.setScrollAnimation(self.SCROLL_DURATION_MS, QEasingCurve.Type.OutCubic)
        # SmoothScrollBar 用一个私有累加器记住滚轮的目标位置，而键盘导航 /
        # scrollTo / 拖动滚动条走的是原生滚动条，不会更新它。不同步的话
        # 「按方向键翻一屏 → 滚一格滚轮」会先跳回翻屏前的位置。
        self.verticalScrollBar().valueChanged.connect(self._sync_wheel_anchor)

    def _sync_wheel_anchor(self, value: int) -> None:
        delegate = getattr(self, "scrollDelegate", None)
        if delegate is None:
            return
        bar = delegate.vScrollBar
        # 动画自己驱动的每一帧也会走到这里，那时不能覆盖它的目标值
        if bar.ani.state() != bar.ani.State.Running:
            bar.resetValue(value)

    def setScrollAnimation(self, duration: int, easing=QEasingCurve.Type.OutCubic) -> None:
        """调整滚轮动画（签名对齐 `SmoothScrollArea.setScrollAnimation` 的后两个参数）。"""
        delegate = getattr(self, "scrollDelegate", None)
        if delegate is not None:
            delegate.vScrollBar.setScrollAnimation(duration, easing)

    # === 定向重绘 ===

    def _row_rect(self, row: int) -> QRect:
        """行的视口矩形；不可见或越界时返回空矩形。"""
        model = self.model()
        if model is None or row < 0 or row >= model.rowCount():
            return QRect()
        index = model.index(row, 0)
        if not index.isValid():
            return QRect()
        # 卡片整个画在 visualRect 内部（delegate 左右内缩 8、上下内缩 4），
        # 所以 ±1 只是抗锯齿的安全余量
        return self.visualRect(index).adjusted(-1, -1, 1, 1)

    def _repaint_rows(self, *rows: int) -> None:
        viewport = self.viewport()
        for row in rows:
            rect = self._row_rect(row)
            if not rect.isEmpty():
                viewport.update(rect)

    def _on_animator_tick(self, rows: set) -> None:
        self._repaint_rows(*rows)

    # === 覆写 ListBase 的三处全视口重绘 ===

    def _setHoverRow(self, row: int) -> None:
        old = getattr(self, "_hover_row", -1)
        if old == row:
            return
        self._hover_row = row
        delegate = self.itemDelegate()
        if hasattr(delegate, "setHoverRow"):
            delegate.setHoverRow(row)
        self._repaint_rows(old, row)

    def _setPressedRow(self, row: int) -> None:
        # 故意不照抄基类的 `if selectionMode() == NoSelection: return`：
        # 按下反馈是纯视觉的，不该和选择模式绑定（阶段 1 仍是 NoSelection）。
        old = getattr(self, "_pressed_row", -1)
        if old == row:
            return
        self._pressed_row = row
        delegate = self.itemDelegate()
        if hasattr(delegate, "setPressedRow"):
            delegate.setPressedRow(row)
        self._repaint_rows(old, row)

    def set_context_row(self, row: int) -> None:
        """标出「正在被右键菜单操作的那一行」。纯视觉，不进选中集合。

        右键不再选中卡片之后，这一行会失去**全部**视觉提示：`RoundMenu` 是个抢鼠标的
        popup，视图立刻收到 `Leave` → `ListBase.leaveEvent` → `_setHoverRow(-1)`，
        delegate 的 `editorEvent(Leave)` 也会清掉自己的 `_hovered_row`。于是菜单里摆着
        「连同文件一起删除」，而用户看不出它要删哪一行 —— 这比原来的 bug 更危险。
        所以另开一个**不受任何 Leave 影响**的通道，由页面在菜单弹出/关闭时置位和复位。
        """
        old = getattr(self, "_context_row", -1)
        if old == row:
            return
        self._context_row = row
        delegate = self.itemDelegate()
        if hasattr(delegate, "set_context_row"):
            delegate.set_context_row(row)
        self._repaint_rows(old, row)

    def updateSelectedRows(self) -> None:
        """覆写为空操作 —— 选中态的重绘改由 `selectionModel().selectionChanged` 驱动。

        基类实现是 `self._setSelectedRows(self.selectedIndexes())`，而
        `selectedIndexes()` 会**物化整个选中集合**。它被 mouseRelease / keyPress /
        clearSelection / setCurrentIndex 四处调用，于是 Ctrl+A 选中一万行之后每次
        松开鼠标都要建一个一万元素的列表。

        更关键的是它根本不够用：`selectAll()` 和 `selectionModel().select()` 这类
        程序化选择**不走这里**。`selectionChanged` 两头都覆盖，而且直接给出「哪些行
        变了」，比事后 diff 全集合更准也更省。
        """

    def _setSelectedRows(self, indexes: list[QModelIndex]) -> None:
        # 上面的 updateSelectedRows() 已经空掉，基类不会再调到这里。保留是为了万一
        # 未来版本的 qfluentwidgets 从别处调用它时不至于 AttributeError。
        delegate = self.itemDelegate()
        if hasattr(delegate, "setSelectedRows"):
            delegate.setSelectedRows(indexes)
        self._apply_selection({index.row() for index in indexes})

    def _on_selection_model_changed(
        self, selected: QItemSelection, deselected: QItemSelection
    ) -> None:
        changed: set[int] = set()
        bulk = False

        # 从 QItemSelection 的区间直接取行号：只包含**本次变化**的行，而且不必像
        # selectedIndexes() 那样把整个选中集合物化出来（Ctrl+A 上万行时那是白花的钱）。
        for selection in (deselected, selected):
            for rng in selection:
                span = range(rng.top(), rng.bottom() + 1)
                if len(span) >= self.BULK_REPAINT_THRESHOLD:
                    bulk = True
                else:
                    changed.update(span)

        self._apply_selection(changed, bulk=bulk)
        self.selection_changed.emit()

    def _apply_selection(self, changed: set[int], bulk: bool = False) -> None:
        """按「本次变化的行」做定向重绘，并同步 delegate 的复选框常显开关。

        故意**不**在本地镜像一份选中行集合：`selectionModel().hasSelection()` 是 O(1)
        且永远是真值，而镜像会在 rowsInserted/rowsRemoved 之后静默错位。
        """
        selection_model = self.selectionModel()
        has_selection = selection_model is not None and selection_model.hasSelection()
        flipped = has_selection != self._has_selection
        self._has_selection = has_selection

        # delegate 靠这个布尔决定复选框是否常显；选中真值它读 option.state。
        # 故意**不**照抄 TableItemDelegate.setSelectedRows 里「选中即复位 pressedRow」的
        # 做法：那是为了让选中底色顶掉按下底色，而我们有独立的 press 动画通道，
        # 复位会让 Shift 连选 / 框选拖到本行时按下反馈中途消失。按下态由
        # mouseReleaseEvent 负责收尾就够了。
        delegate = self.itemDelegate()
        if hasattr(delegate, "set_selection_active"):
            delegate.set_selection_active(has_selection)

        # 「有没有选中项」一翻转，复选框在**每一行**上的可见性都会变 —— 只能整屏。
        if bulk or flipped or len(changed) >= self.BULK_REPAINT_THRESHOLD:
            self.viewport().update()
            return
        if changed:
            self._repaint_rows(*changed)

    def mousePressEvent(self, e) -> None:
        # 一按下就收气泡：接下来可能是勾选、拖拽框选或弹菜单，那时提示只是挡视线
        self.hide_tooltip()
        # 在交给基类之前先做命中测试：松开时视图已经不知道这一串手势是从哪儿起的了。
        self._press_on_control = self._is_control_at(e.pos())
        self._press_pos = e.pos()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        super().mouseReleaseEvent(e)
        # 基类只在「点空白处或右键」时复位；正常点在行上会让 pressedRow 永久卡住
        # （updateSelectedRows 已被我们空掉，没人替它复位）。
        self._setPressedRow(-1)
        # 必须在 super() 之后清 —— selectionCommand 是在 super() 内部被问到的
        self._press_on_control = False
        self._press_pos = None

    # === 点击切换选中 ===

    def _is_control_at(self, pos: QPoint) -> bool:
        """坐标是否落在某一行的复选框/按钮上。delegate 没有子控件，只能反查它。"""
        delegate = self.itemDelegate()
        if not hasattr(delegate, "is_interactive_at"):
            return False
        index = self.indexAt(pos)
        if not index.isValid():
            return False
        return bool(delegate.is_interactive_at(self.visualRect(index), pos))

    def _is_drag_gesture(self, event) -> bool:
        """这一串手势到底是「点击」还是「框选拖拽」。

        **故意不用 `state() == DragSelectingState` 判断**：基类的 `mouseMoveEvent` 只要左键
        按着动一下就置位它，于是真人手抖 2px 也会被算成拖拽 —— 那会让「再点一下取消勾选」
        在真机上时灵时不灵，而 `QTest.mouseClick` 根本不发 move 事件，测试里反而永远看不到
        这个坑。改用位移阈值：低于 `startDragDistance()` 一律当点击。
        """
        press = self._press_pos
        if press is None:
            return False
        return (event.pos() - press).manhattanLength() > QApplication.startDragDistance()

    def selectionCommand(
        self,
        index: QModelIndex | QPersistentModelIndex,
        /,
        event: QEvent | None = None,
    ) -> QItemSelectionModel.SelectionFlag:
        """左键单击卡片 == 纯粹的勾选/取消勾选：勾选就勾选，取消就取消，别的行永不受影响。

        `ExtendedSelection` 的原生语义是资源管理器语义：无修饰键单击**未选中**行返回
        `ClearAndSelect`（先清空再选中），单击**已选中**行则把多选收窄成一行。落在这个页面
        上就是「勾了五个，再点第六张卡，前五个全没了」。这里把它整体换成 `Toggle`。

        分三种事件回答：

        - **按下未选中行** → `Toggle`（基类当场把它翻成 `Select`）。按下即反馈，而且
          `noSelectionOnMousePress` 保持 False，松开时基类不会再问一次，天然不会翻两下。
        - **按下已选中行** → `NoUpdate`，把结论推到松开时 —— 这是 Qt 留给拖拽的窗口，原生
          也是这么做的。松开时若确认是点击就 `Toggle` 取消勾选；若是拖拽则 `NoUpdate`，
          框选已经在移动过程中生效了，不该反手把起点那一行取消掉。
        - **拖动中（框选）** → `Select`，只增不减。基类在 `DragSelectingState` 下返回的是
          `ClearAndSelect`，那意味着框选一起手就清空既有勾选。代价是橡皮筋往回收时不实时
          取消，换来「框选永远不会毁掉已有的批量」。

        按在卡片里的控件（复选框 / 三颗按钮）上时**整个选择集合都不动**，而且必须显式返回
        `NoUpdate`：按下时 delegate 的 `editorEvent` 返回 True 让 `mousePressEvent` 就地
        return，但 `noSelectionOnMousePress` **在那之前**已经置位，而 `mouseReleaseEvent` 里
        那句 `edited = click ? edit(index, trigger, event) : false` 无论如何都算出
        `edited == False` —— 于是基类照样会在松开时返回 `ClearAndSelect`，选了十行再点其中
        一行的「打开文件夹」，选中集合当场塌成一行。复选框自身的切换走 delegate 的
        `selection_toggled` 信号（同样是 `Toggle`），不经过这里。

        Shift / Ctrl / 键盘一律交回基类：Shift 是 `SelectCurrent`（带 `Current` 位，锚点不动
        且只增不减），Ctrl 原生就是 `Toggle`。
        点空白处仍然清空，这是唯一还会清掉整批勾选的手势，与批量条的「取消选择」互补。

        **右键（以及中键）在任何情况下都返回 `NoUpdate`**，选中集合一动不动。这一条和
        `setSelectRightClickedRow(False)` 各自单独就够拦住那个 bug（实测拆掉任意一道另一道
        仍然守得住，两道都拆才复现），留着两道是因为它们覆盖的是**不同的路径**：那个开关只
        管按下，而 `QAbstractItemView::mouseReleaseEvent` 在 `noSelectionOnMousePress` 置位
        时会**再问一次** `selectionCommand`，那条路不看 `_isSelectRightClickedRow`。
        右键菜单的作用域改由页面按「被右键的行是否已勾选」判定
        （见 `UnifiedTaskListPage._menu_scope`），不需要靠改选中来表达。
        """
        if event is None:
            return super().selectionCommand(index, event)

        etype = event.type()
        no_modifier = event.modifiers() == Qt.KeyboardModifier.NoModifier
        toggle = QItemSelectionModel.SelectionFlag.Toggle | QItemSelectionModel.SelectionFlag.Rows

        # 非左键的按下/松开/双击：绝不改选择。放在最前面，`_press_on_control` 之类的
        # 状态都还没参与判断 —— 右键本来就不该走那套。
        if etype in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        ) and event.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton):
            return QItemSelectionModel.SelectionFlag.NoUpdate

        # 框选途中：只增不减（基类在这里会返回 ClearAndSelect）
        if etype == QEvent.Type.MouseMove:
            if no_modifier and event.buttons() == Qt.MouseButton.LeftButton:
                if self._press_on_control:
                    return QItemSelectionModel.SelectionFlag.NoUpdate
                return (
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows
                )
            return super().selectionCommand(index, event)

        if etype not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            return super().selectionCommand(index, event)
        if not index.isValid() or not no_modifier or event.button() != Qt.MouseButton.LeftButton:
            return super().selectionCommand(index, event)
        if self._press_on_control:
            return QItemSelectionModel.SelectionFlag.NoUpdate

        if etype == QEvent.Type.MouseButtonPress:
            selection_model = self.selectionModel()
            if selection_model is not None and selection_model.isSelected(index):
                # 已选中 → 让出拖拽窗口，结论留给松开时
                return QItemSelectionModel.SelectionFlag.NoUpdate
            return toggle
        if self._is_drag_gesture(event):
            return QItemSelectionModel.SelectionFlag.NoUpdate
        return toggle

    # === 右键菜单 ===

    def contextMenuEvent(self, e) -> None:
        # 到这里选中集合**没有被右键动过**（见 __init__ 里 setSelectRightClickedRow(False)
        # 和 selectionCommand 的非左键分支）。菜单的作用域由页面决定：
        # 右键的是已勾选行 → 作用于整批；是未勾选行 → 只作用于它一行，且不改勾选。
        #
        # 顺手清掉按下态：关掉 setSelectRightClickedRow 之后，右键按下会走
        # `ListBase.mousePressEvent` 的第二条分支（`_setPressedRow(index.row())`），
        # 而菜单一弹出就抢走鼠标 —— Windows 上 WM_CONTEXTMENU 在 button-up 之后到，
        # 松开能回到视图；别的平台则是按下时就合成 contextMenuEvent，那条路的 release
        # 会被 popup 吃掉，按下态就永久卡在那一行上。右键的视觉提示交给 `_context_row`。
        self._setPressedRow(-1)
        # 气泡是 WindowStaysOnTopHint 的，留着会浮在右键菜单上面
        self.hide_tooltip()
        index = self.indexAt(e.pos())
        self.context_menu_requested.emit(index.row() if index.isValid() else -1, e.globalPos())

    # === Delegate / Model 挂接 ===

    def setItemDelegate(self, delegate) -> None:  # type: ignore[override]
        super().setItemDelegate(delegate)
        self._wire_animator(delegate)

    def _wire_animator(self, delegate) -> None:
        animator = getattr(delegate, "animator", None)
        previous = getattr(self, "_animator", None)
        if animator is previous:
            return
        if previous is not None:
            try:
                previous.ticked.disconnect(self._on_animator_tick)
            except (RuntimeError, TypeError):
                pass
        self._animator = animator
        if animator is not None:
            animator.ticked.connect(self._on_animator_tick)

    def setModel(self, model) -> None:  # type: ignore[override]
        old = self.model()
        if old is not None:
            for signal in (old.rowsInserted, old.rowsRemoved, old.modelReset, old.layoutChanged):
                try:
                    signal.disconnect(self._on_rows_restructured)
                except (RuntimeError, TypeError):
                    pass
        super().setModel(model)
        if model is not None:
            for signal in (
                model.rowsInserted,
                model.rowsRemoved,
                model.modelReset,
                model.layoutChanged,
            ):
                signal.connect(self._on_rows_restructured)
        # QAbstractItemView.setModel 会**新建**一个 QItemSelectionModel，所以这条连接
        # 必须在 super().setModel() 之后重做，否则换模型后选中变化就再也传不出来了。
        selection_model = self.selectionModel()
        if selection_model is not None:
            selection_model.selectionChanged.connect(self._on_selection_model_changed)
        self._on_rows_restructured()

    def _on_rows_restructured(self, *args) -> None:
        """行号发生平移 → 动画状态必须吸附，否则 A 行的过渡会跑到 B 行上。"""
        self._hover_row = -1
        self._pressed_row = -1
        # 右键高亮同理：`add_task` 插在第 0 行会把所有行号推下去，留着就是高亮错行 ——
        # 而菜单要操作哪一行是用 `QPersistentModelIndex` 记的，Qt 会自己跟着平移，
        # 所以这里丢掉视觉提示是安全的降级，指错行才不是。
        self.set_context_row(-1)
        # 气泡的锚点是当时那一行的 visualRect，行号一平移它就指向别人了
        self.hide_tooltip()
        # 插入/删除会让选中项的行号整体平移，但 Qt 的 selectionModel 会自己跟着调整，
        # 所以「有没有选中项」直接问它就是真值（O(1)，不必物化选中集合）。
        selection_model = self.selectionModel()
        self._has_selection = selection_model is not None and selection_model.hasSelection()
        delegate = self.itemDelegate()
        if hasattr(delegate, "set_selection_active"):
            delegate.set_selection_active(self._has_selection)
        animator = getattr(self, "_animator", None)
        if animator is None:
            return
        animator.snap_all()
        model = self.model()
        animator.prune(model.rowCount() if model is not None else 0)

    # === Tooltip ===
    #
    # 为什么不是 `QToolTip.showText()`：那是**裸 Qt 控件**，qfluentwidgets 不给它上样式表，
    # 于是在这个应用的暗色主题下它就是一个纯黑方块（用户看到的正是这个）。项目里另外
    # 十几处提示全都走 `ToolTipFilter` → `ToolTip`，那条路由 `FluentStyleSheet.TOOL_TIP`
    # 上色并跟随 `qconfig.themeChanged`，明暗两套都对（CLAUDE.md §3：禁止裸 Qt 控件、
    # 禁止硬编码颜色）。
    #
    # 但 `ToolTipFilter` 是挂在**真实控件**上的（靠 Enter/Leave + `widget.toolTip()`），
    # delegate 画的卡片没有子控件，所以这里自己驱动一个 `ToolTip` 实例：文案与锚点向
    # delegate 反查（`tooltip_at`），触发时机仍旧交给 Qt 的 `QEvent.ToolTip`——
    # 悬停唤醒延迟由平台风格给，不必自己再养一个定时器。
    # `ItemViewToolTip` / `ItemViewToolTipManager` 更贴近这个场景，但它们**没有从
    # qfluentwidgets 包顶层导出**，只能走私有模块路径，所以定位自己算（见 _tooltip_pos）。

    def viewportEvent(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            if self._show_tooltip(event):
                return True
        return super().viewportEvent(event)

    def hide_tooltip(self) -> None:
        if self._tooltip is not None:
            self._tooltip.hide()
        self._tooltip_text = ""

    def _on_scrolled(self, _value: int) -> None:
        self.hide_tooltip()

    def _tooltip_widget(self) -> ToolTip:
        if self._tooltip is None:
            tip = ToolTip(parent=self.window())
            tip.setDuration(self.TOOLTIP_DURATION_MS)
            # 量一次「窗口比文字标签宽多少」：两层 layout 边距 + container 的边框。
            # 照抄上游的 (12,8,12,12)+(8,6,8,6) 会漏掉边框（实测合计 42 而非 40），
            # 而且上游改样式时会静默漂移。
            tip.label.setWordWrap(False)
            tip.setText("M")
            self._tooltip_chrome = max(0, tip.width() - tip.label.sizeHint().width())
            self._tooltip = tip
        return self._tooltip

    def _tooltip_text_width(self) -> int:
        """文字标签的宽度上限。上限本身还要让位给屏幕 —— 比屏幕还宽的气泡夹不回来。"""
        limit = self.TOOLTIP_MAX_WIDTH
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            limit = min(limit, max(160, screen.availableGeometry().width() - 64))
        return max(80, limit - self._tooltip_chrome)

    def _set_tooltip_text(self, tip: ToolTip, text: str) -> None:
        """设置文案：放得下就单行，放不下就折行，折不开就省略。

        `ToolTip` 的 label 不折行 —— 一个 98 字的标题会排成 1176px 宽的一条，比 1024 宽的
        屏幕还长，而 `_tooltip_pos` 的夹取救不了比屏幕还宽的窗口。项目里其余十几处提示都是
        短文案，所以上游从来不需要处理这件事，这里得自己来。

        三处细节都是实测踩出来的，别照直觉简化：

        - 量「自然宽度」之前必须先清掉上一次的固定尺寸，否则 `sizeHint()` 被夹住，短文案
          会量出上一条长标题的宽度，然后顶着上限显示一个空荡荡的大框；
        - 折行时高度自己算：wordWrap + 固定宽度下 label 的 `sizeHint()` 走的是
          `sizeForWidth(-1)` 的黄金比例猜测，`setFixedWidth` 之后它自报只有一行高
          （实测 98 字的标题该 3 行、label 只有 14px），要等布局跑一遍才补成 40px。
          窗口高度靠 `adjustSize()` 是对的、位置也不受影响，但显式给高度能让几何在 show
          之前就自洽，且不必赌 `QHBoxLayout` 会照顾 `heightForWidth`；
        - `Qt::TextWordWrap` 拆不开中间没有断点的长串（比如一个 400 字符的连续 token），
          这种只能退回单行省略号 —— 它本来也没法在一个气泡里读完。
        """
        label = tip.label
        label.setWordWrap(False)
        label.setMinimumSize(0, 0)
        label.setMaximumSize(_QWIDGETSIZE_MAX, _QWIDGETSIZE_MAX)
        label.setText(text)

        limit = self._tooltip_text_width()
        if label.sizeHint().width() <= limit:
            label.setFixedSize(label.sizeHint())
            tip.setText(text)
            return

        fm = label.fontMetrics()
        wrapped = fm.boundingRect(
            QRect(0, 0, limit, _WRAP_PROBE_HEIGHT), Qt.TextFlag.TextWordWrap, text
        )
        if wrapped.width() <= limit:
            label.setWordWrap(True)
            label.setFixedSize(limit, label.heightForWidth(limit))
            tip.setText(text)
            return

        label.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, limit))
        label.setFixedSize(label.sizeHint())
        tip.setText(label.text())

    def _tooltip_pos(self, tip: ToolTip, anchor: QRect) -> QPoint:
        """把气泡贴在锚定区域**上方居中**，再夹回屏幕可用区内。

        纵向减的是 `height - 10` 而不是 `height`：`ToolTip` 的外层 layout 留了
        (12, 8, 12, 12) 的边距给投影，可见的圆角框比窗口本身小一圈，直接按窗口高度
        摆会离锚点浮起十几像素。这个 10 与 qfluentwidgets 自己的
        `ItemViewToolTipManager` 取的是同一个值。
        """
        # anchor 来自 visualRect()，是**视口**坐标，所以必须由 viewport 来 mapToGlobal
        top_left = self.viewport().mapToGlobal(anchor.topLeft())
        x = top_left.x() + anchor.width() // 2 - tip.width() // 2
        y = top_left.y() - tip.height() + 10

        screen = QApplication.screenAt(top_left) or QApplication.primaryScreen()
        if screen is not None:
            bounds = screen.availableGeometry()
            x = max(bounds.left(), min(x, bounds.right() - tip.width() + 1))
            if y < bounds.top():
                # 上方放不下（第一行贴着屏幕顶端）就翻到锚点下方
                y = top_left.y() + anchor.height() - 8
            y = max(bounds.top(), min(y, bounds.bottom() - tip.height() + 1))
        return QPoint(x, y)

    def _show_tooltip(self, event) -> bool:
        delegate = self.itemDelegate()
        if not hasattr(delegate, "tooltip_at"):
            return False
        pos = event.pos()
        index = self.indexAt(pos)
        if not index.isValid():
            self.hide_tooltip()
            return False
        text, anchor = delegate.tooltip_at(self.visualRect(index), pos, index)
        if not text or anchor.isEmpty():
            self.hide_tooltip()
            return False

        tip = self._tooltip_widget()
        if text != self._tooltip_text:
            # 换文案先收起来：`show()` 对已经显示的窗口不会再走一遍 showEvent，
            # 自动隐藏计时器和 150ms 淡入都不会重置。
            tip.hide()
            # 会 adjustSize()，必须在算位置之前
            self._set_tooltip_text(tip, text)
            # 存原始文案（而不是可能被省略过的显示文案），否则换行判断会来回抖
            self._tooltip_text = text
        tip.move(self._tooltip_pos(tip, anchor))
        tip.show()
        return True

    def leaveEvent(self, e) -> None:
        super().leaveEvent(e)
        self.hide_tooltip()

    def hideEvent(self, e) -> None:
        # 切筛选 Tab / 切页面时视图被隐藏，而气泡是独立的顶层窗口，不会跟着消失
        self.hide_tooltip()
        super().hideEvent(e)

    # === 键盘 ===

    def keyPressEvent(self, e) -> None:
        super().keyPressEvent(e)
        # 基类的 keyPressEvent 会调 updateSelectedRows() → _setSelectedRows()，
        # 已经走定向重绘，这里只需要跟随键盘焦点移动悬停高亮。
        if e.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End):
            current = self.currentIndex()
            if current.isValid():
                self._setHoverRow(current.row())
