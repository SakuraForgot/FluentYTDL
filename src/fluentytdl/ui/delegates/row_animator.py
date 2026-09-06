"""共享的行动画插值器：目标趋近式，天生可打断。

为什么不用 QPropertyAnimation
------------------------------
项目里已经有前车之鉴 —— `ui/components/common/interruptible_navigation.py` 存在的唯一
原因就是原生动画在「中途反向」时会丢帧/跳变。鼠标快速划过列表时每一行都要反向一次，
同样的问题必然复现，而且是 N 行同时复现。

这里改成每帧朝目标做指数趋近：

    value += (target - value) * (1 - exp(-dt / TAU))

改变 target 就等于重定向，没有需要 cancel 的动画对象 —— 打断问题从根上消失。
一个共享的 16ms QTimer 驱动所有行；没有未收敛的通道时定时器停摆，空闲开销为零。

所有插值量都约定在 0..1 区间（进度请先除以 100），这样单一 EPS 阈值对全部通道成立。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QElapsedTimer, QObject, Qt, QTimer, Signal

# 通道名常量：避免各处手写字符串拼错
CH_HOVER = "hover"
CH_PRESS = "press"
CH_SELECT = "select"
CH_PROGRESS = "progress"
# 复选框的淡入淡出。常驻多选下复选框不再由一个全局开关控制，而是
# 「悬停该行 / 该行已选中 / 列表里已有选中项」三者任一成立时淡入。
CH_CHECK = "check"


class RowAnimator(QObject):
    """按 (row, channel) 维护插值状态，并在每帧广播「哪些行需要重绘」。"""

    # 本帧发生变化的行号集合。View 负责把它翻译成定向 viewport().update(visualRect)
    ticked = Signal(set)

    # TAU=40ms → 3τ≈120ms 达到 95%，与 CardWidget.setDuration(120) 观感对齐
    TAU_MS = 40.0
    # 收敛阈值。0..1 区间下 0.004 已远低于一个像素的可见差异
    EPS = 0.004
    # 单帧最大步长：主线程被长任务卡住时不要一次跳完，也不要算出巨大的 k
    _MAX_DT_MS = 100.0

    def __init__(self, parent: QObject | None = None, interval_ms: int = 16) -> None:
        super().__init__(parent)

        self._value: dict[tuple[int, str], float] = {}
        self._target: dict[tuple[int, str], float] = {}
        # 未收敛的通道；空集合 → 停表
        self._active: set[tuple[int, str]] = set()
        # 键的「世代」。结构变更时只递增 _gen，不清空数值 —— 清空会让每次插入行都把
        # 所有进度条从 0 重新填一遍。世代不匹配的键在下一次 approach 时直接吸附到真值。
        self._gen_of: dict[tuple[int, str], int] = {}
        self._gen = 0

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(max(1, interval_ms))
        self._timer.timeout.connect(self._on_tick)

        self._clock = QElapsedTimer()

    # === 供 delegate.paint() 调用 ===

    def approach(self, row: int, channel: str, target: float) -> float:
        """登记目标值并返回**当前**应绘制的值。

        新键（或结构变更后第一次触碰）直接吸附到 target：刚进入视口的行不应该
        从 0 爬上来。
        """
        key = (row, channel)

        if self._gen_of.get(key) != self._gen:
            self._gen_of[key] = self._gen
            self._value[key] = target
            self._target[key] = target
            self._active.discard(key)
            return target

        self._target[key] = target
        current = self._value.get(key, target)

        if abs(target - current) < self.EPS:
            self._value[key] = target
            self._active.discard(key)
            if not self._active:
                self._timer.stop()
            return target

        self._value[key] = current
        self._active.add(key)
        if not self._timer.isActive():
            self._clock.restart()
            self._timer.start()
        return current

    def value(self, row: int, channel: str, default: float = 0.0) -> float:
        """只读当前值，不登记目标（用于跨通道联动）。"""
        return self._value.get((row, channel), default)

    def approach_lazy(self, row: int, channel: str, target: float) -> float:
        """`approach` 的省键版本：目标就是静息值 0 且该键从未被触碰时，不建键。

        delegate 每帧要为每行登记 7 个通道（hover/press/select/progress + 3 个按钮），
        而其中绝大多数一辈子都停在 0 —— 1000 行会白白留下 7000 个字典项。
        """
        if target == 0.0 and (row, channel) not in self._value:
            return 0.0
        return self.approach(row, channel, target)

    # === 生命周期维护 ===

    def snap_all(self) -> None:
        """结构变更（插入/删除/重置）：让所有键在下次 approach 时吸附，不做过渡动画。

        行号在插入后会整体平移，继续沿用旧值等于把 A 行的动画放到 B 行上。
        """
        self._gen += 1
        self._active.clear()
        self._timer.stop()

    def prune(self, row_count: int) -> None:
        """丢弃行号已越界的键，避免长会话里字典单调增长。"""
        stale = [key for key in self._value if key[0] >= row_count]
        for key in stale:
            self._value.pop(key, None)
            self._target.pop(key, None)
            self._gen_of.pop(key, None)
            self._active.discard(key)
        if not self._active:
            self._timer.stop()

    def clear(self) -> None:
        self._value.clear()
        self._target.clear()
        self._gen_of.clear()
        self._active.clear()
        self._timer.stop()

    # === 驱动 ===

    def _on_tick(self) -> None:
        dt = float(self._clock.restart())
        if dt <= 0.0:
            dt = 1.0
        dt = min(dt, self._MAX_DT_MS)
        k = 1.0 - math.exp(-dt / self.TAU_MS)

        rows: set[int] = set()
        for key in tuple(self._active):
            current = self._value.get(key, 0.0)
            target = self._target.get(key, current)
            current += (target - current) * k
            if abs(target - current) < self.EPS:
                current = target
                self._active.discard(key)
            self._value[key] = current
            rows.add(key[0])

        if not self._active:
            self._timer.stop()

        if rows:
            self.ticked.emit(rows)
