"""底部悬浮批量操作条。

常驻多选下它不再是一个「模式」的 UI —— 选中集合非空就滑入、归零就滑出，
由 `UnifiedTaskListPage` 接 `TaskListView.selection_changed` 驱动。
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout
from qfluentwidgets import (
    Action,
    BodyLabel,
    FluentIcon,
    RoundMenu,
    TransparentDropDownToolButton,
    TransparentToolButton,
    isDarkTheme,
)


class BatchOperationPanel(QFrame):
    batch_start_requested = Signal()  # 开始选中任务
    batch_pause_requested = Signal()  # 暂停选中任务
    batch_delete_requested = Signal(bool)  # 删除选中任务, bool=是否删文件
    select_all_requested = Signal()  # 全选（只作用于当前筛选可见的行）
    clear_selection_requested = Signal()  # 取消选择

    # 距父窗口底边的距离
    BOTTOM_MARGIN = 30
    # 进场与退场**必须**是两条不同的曲线。Fluent 的动效约定是「进场减速、退场加速」，
    # 两个方向共用一条 OutCubic + 同一个时长，看上去就是「呼出还是关闭都一样」。
    SLIDE_IN_DURATION_MS = 280
    SLIDE_OUT_DURATION_MS = 160
    # OutBack 的回弹量。Qt 默认的 1.70158 在 86px 行程上要顶出约 9px，太跳；
    # 1.2 约 6px，是「弹上来」而不是「甩上来」。
    SLIDE_IN_OVERSHOOT = 1.2
    # 中途反向时按剩余行程缩短时长。否则「刚露头就取消选择」要用整整 160ms 去走
    # 那几个像素，慢得像卡住了。
    MIN_DURATION_RATIO = 0.35

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BatchOperationPanel")

        self.setFixedHeight(56)

        self._init_ui()
        self._update_style()

        # 滑动动画。**不要**改成 QGraphicsOpacityEffect 或 DropShadowEffect ——
        # 见 _update_style() 末尾的注释，阴影 effect 曾导致隐藏时 Qt segfault。
        # 时长与曲线由 _start_slide() 按方向逐次设置，所以这里不预设。
        self._slide_ani = QPropertyAnimation(self, b"pos", self)
        self._slide_ani.finished.connect(self._on_slide_finished)
        # 动画的**目标**可见性。isVisible() 在滑出过程中仍是 True，判断「现在该去哪」
        # 只能靠这个标记。
        self._shown = False
        self.hide()

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)

    def _init_ui(self):
        self.h_layout = QHBoxLayout(self)
        self.h_layout.setContentsMargins(16, 0, 16, 0)
        self.h_layout.setSpacing(12)

        # === 选中计数 ===
        self.count_label = BodyLabel(self.tr("已选择 0 项"), self)
        self.count_label.setMinimumWidth(130)  # 防止字数多时挤压
        self.h_layout.addWidget(self.count_label)

        # === 全选 ===
        self.btn_select_all = TransparentToolButton(FluentIcon.ACCEPT, self)
        self.btn_select_all.setToolTip(self.tr("全选当前筛选结果"))
        self.btn_select_all.clicked.connect(self.select_all_requested)
        self.h_layout.addWidget(self.btn_select_all)

        # 分隔符
        self.separator1 = QFrame(self)
        self.separator1.setFrameShape(QFrame.Shape.VLine)
        self.separator1.setFrameShadow(QFrame.Shadow.Plain)
        self.separator1.setFixedWidth(1)
        self.h_layout.addWidget(self.separator1)

        # === 动作按钮 ===
        self.btn_start = TransparentToolButton(FluentIcon.PLAY, self)
        self.btn_start.setToolTip(self.tr("开始选中"))
        self.btn_start.clicked.connect(self.batch_start_requested)
        self.h_layout.addWidget(self.btn_start)

        self.btn_pause = TransparentToolButton(FluentIcon.PAUSE, self)
        self.btn_pause.setToolTip(self.tr("暂停选中"))
        self.btn_pause.clicked.connect(self.batch_pause_requested)
        self.h_layout.addWidget(self.btn_pause)

        # === 删除下拉菜单 ===
        self.btn_delete = TransparentDropDownToolButton(FluentIcon.DELETE, self)
        self.btn_delete.setToolTip(self.tr("删除选中"))

        menu = RoundMenu(parent=self)
        self.action_del_record = Action(FluentIcon.DOCUMENT, self.tr("仅删记录"), self)
        self.action_del_all = Action(FluentIcon.DELETE, self.tr("连同文件一起删除"), self)

        self.action_del_record.triggered.connect(lambda: self.batch_delete_requested.emit(False))
        self.action_del_all.triggered.connect(lambda: self.batch_delete_requested.emit(True))

        menu.addAction(self.action_del_record)
        menu.addAction(self.action_del_all)
        self.btn_delete.setMenu(menu)

        self.h_layout.addWidget(self.btn_delete)

        # 分隔符
        self.separator2 = QFrame(self)
        self.separator2.setFrameShape(QFrame.Shape.VLine)
        self.separator2.setFrameShadow(QFrame.Shadow.Plain)
        self.separator2.setFixedWidth(1)
        self.h_layout.addWidget(self.separator2)

        # === 取消选择 ===
        # 常驻多选下「退出批量」已经无处可退（没有模式了），这颗按钮的语义就是清空选中集合，
        # 与原来的「取消全选」合并成一颗 —— 两颗做同一件事只会让人犹豫该按哪个。
        self.btn_clear = TransparentToolButton(FluentIcon.CANCEL, self)
        self.btn_clear.setToolTip(self.tr("取消选择"))
        self.btn_clear.clicked.connect(self.clear_selection_requested)
        self.h_layout.addWidget(self.btn_clear)

    def _update_style(self):
        bg_color = "rgba(43, 43, 43, 0.96)" if isDarkTheme() else "rgba(255, 255, 255, 0.96)"
        border_color = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.06)"

        self.setStyleSheet(f"""
            BatchOperationPanel {{
                background-color: {bg_color};
                border: 1px solid {border_color};
                border-radius: 28px;
            }}
        """)

        # 分隔线颜色
        sep_color = "rgba(255, 255, 255, 0.15)" if isDarkTheme() else "rgba(0, 0, 0, 0.1)"
        self.separator1.setStyleSheet(f"background-color: {sep_color}; border: none;")
        self.separator2.setStyleSheet(f"background-color: {sep_color}; border: none;")

        # 移除 QGraphicsDropShadowEffect 防止 Qt 在隐藏时发生 Segfault
        # 改为通过 qss 加强边框和背景来弥补视觉效果

    def update_count(self, selected_count: int, total_count: int):
        self.count_label.setText(f"已选择 {selected_count} / {total_count} 项")

    # === 滑入 / 滑出 ===

    def relayout(self) -> None:
        """父窗口尺寸变化时重新摆位。页面的 resizeEvent 只需要调这一个方法。"""
        if self.parentWidget() is None:
            return
        self._fit_width()
        target = self._resting_pos() if self._shown else self._hidden_pos()
        if self._slide_ani.state() == QPropertyAnimation.State.Running:
            # 尺寸变了终点也就变了，从当前位置续上，别让动画停在旧终点
            self._start_slide(target, entering=self._shown)
        else:
            self.move(target)

    def slide_in(self) -> None:
        """露出面板。**选中集合每变一次都会调到这里**，所以它必须是幂等的。"""
        if self.parentWidget() is None:
            return
        # 先记下「进这个方法之前是不是已经稳稳停在位上」—— 下面会改 _shown
        settled = self._shown and self._slide_ani.state() != QPropertyAnimation.State.Running
        self._fit_width()
        if not self.isVisible():
            # 先摆到屏幕外再 show()，否则会先在终点闪一帧
            self.move(self._hidden_pos())
            self.show()
        self._shown = True
        self.raise_()
        target = self._resting_pos()
        if settled:
            # 已经在位上，这一次只是选中数变了。文案变宽会让居中位置漂几个像素，直接
            # move 过去：重跑一遍进场动画等于把「改数字」演成「重新弹出」，而框选拖拽
            # 每帧都要走到这里 —— 那就是「卡卡的」的来源。
            self.move(target)
            return
        self._start_slide(target, entering=True)

    def slide_out(self) -> None:
        self._shown = False
        if not self.isVisible():
            # 页面不在前台（父窗口整条链隐藏）时没必要演动画，但**必须**把位置和可见性
            # 一并收干净：直接 return 会留下「几何还停在位上、_shown 已经是 False」的
            # 错位状态，等页面切回来时会先闪一帧才被 relayout() 拽下去。
            self.move(self._hidden_pos())
            self.hide()
            return
        self._start_slide(self._hidden_pos(), entering=False)

    def _fit_width(self) -> None:
        # 只在宽度真的变了才 resize：resize() 会让整条面板重新布局并重绘，
        # 而这个方法在框选拖拽时是每帧都会被调到的。
        width = self.sizeHint().width() + 20
        if width != self.width():
            self.resize(width, self.height())

    def _resting_pos(self) -> QPoint:
        parent = self.parentWidget()
        if parent is None:
            return self.pos()
        return QPoint(
            (parent.width() - self.width()) // 2,
            parent.height() - self.height() - self.BOTTOM_MARGIN,
        )

    def _hidden_pos(self) -> QPoint:
        """完全落在父窗口下缘之外的位置。"""
        parent = self.parentWidget()
        resting = self._resting_pos()
        bottom = parent.height() if parent is not None else resting.y() + self.height()
        return QPoint(resting.x(), bottom)

    def _start_slide(self, end: QPoint, entering: bool) -> None:
        ani = self._slide_ani
        if ani.state() == QPropertyAnimation.State.Running:
            if ani.endValue() == end:
                # 已经在往同一个终点走。这时重启会把 startValue 挪到当前位置并把时长
                # 重置成满值，于是每次调用都把它按回「刚起步」—— 动画永远走不到头。
                return
        elif self.pos() == end:
            return

        start = self.pos()
        # 按剩余行程缩放时长，中途反向才不会显得拖沓
        full = max(1, abs(self._hidden_pos().y() - self._resting_pos().y()))
        ratio = min(1.0, max(self.MIN_DURATION_RATIO, abs(end.y() - start.y()) / full))

        curve = QEasingCurve(QEasingCurve.Type.OutBack if entering else QEasingCurve.Type.InCubic)
        if entering:
            curve.setOvershoot(self.SLIDE_IN_OVERSHOOT)

        base = self.SLIDE_IN_DURATION_MS if entering else self.SLIDE_OUT_DURATION_MS
        # 中途 stop() 不会发 finished（Qt 只在动画真正走到终点时发），所以这里重启是安全的
        ani.stop()
        ani.setDuration(int(base * ratio))
        ani.setEasingCurve(curve)
        ani.setStartValue(start)
        ani.setEndValue(end)
        ani.start()

    def _on_slide_finished(self) -> None:
        # 只在滑出结束后才真正 hide()：提前 hide 会让「滑出」变成「瞬间消失」
        if not self._shown:
            self.hide()
