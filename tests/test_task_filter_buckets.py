"""筛选桶 → 状态集合映射的无头测试。

阶段 4 把任务列表的 7 个 Tab 精简成「4 主桶 + 更多筛选」。精简的风险是**把状态弄丢**：
旧设计正是既多又缺 —— `quality_guard` 有 Tab 但 delegate 没有对应文案，`cancelled`
连 Tab 都没有，只能在「全部」里翻。所以这里钉住三条结构性质：

1. **划分**：三个非「全部」主桶恰好把 `tasks.state` 的取值全集分完 —— 每个状态有且只有
   一个归属，于是「全部」的计数永远等于三个主桶之和，用户不会看到对不上的数字；
2. **闭合**：没有筛选项引用不存在的状态（写错一个字符串就是一个永远筛不出东西的入口）；
3. **计数域由主桶推导**：`_MEMORY_COUNT_STATES` / `_DB_COUNT_STATES` 不是另抄的字面量，
   往桶里加状态时计数会自动跟上。

纯常量断言，不建 Qt 对象、不碰 DB —— 但导入页面模块会连带导入 `task_db` 单例（它在
`download_list_model` 里是模块级导入），所以数据根照例先指到临时目录。
"""

import os
import sys
import tempfile
from pathlib import Path

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 导入页面 → 导入 download_list_model → 导入 task_db 单例，会按真实规则解析用户数据目录
_ENV_GUARD = tempfile.mkdtemp(prefix="fluentytdl-buckets-test-")
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", _ENV_GUARD)

from fluentytdl.storage.task_db import TERMINAL_STATES, UNFINISHED_STATES  # noqa: E402
from fluentytdl.ui.unified_task_list_page import (  # noqa: E402
    _DB_COUNT_STATES,
    _FILTER_STATES,
    _MAIN_BUCKETS,
    _MEMORY_COUNT_STATES,
    _SECONDARY_FILTERS,
)

# `tasks.state` 的取值全集，来自 storage 层的权威声明（写入方只有 db_writer 和
# `suspend_pending`），不在测试里另列一份。
ALL_STATES = frozenset(TERMINAL_STATES) | frozenset(UNFINISHED_STATES)
# 「全部」是「不按状态筛」，不参与划分
SORTING_BUCKETS = tuple(b for b in _MAIN_BUCKETS if b != "all")


# === 声明的完整性 ===


def test_every_declared_filter_has_a_state_set():
    """pivot 项和下拉项都得能查到状态集合，否则点下去是个死筛选。"""
    declared = set(_MAIN_BUCKETS) | set(_SECONDARY_FILTERS)
    assert declared == set(_FILTER_STATES), (
        f"缺少映射: {declared - set(_FILTER_STATES)}；多余映射: {set(_FILTER_STATES) - declared}"
    )


def test_all_bucket_is_the_unfiltered_one():
    """空集合是「不按状态筛」的约定值，不是「筛不出任何东西」。"""
    assert _FILTER_STATES["all"] == frozenset()


def test_no_filter_references_an_unknown_state():
    """状态名是字符串，写错一个字符就得到一个永远空的筛选项。"""
    for name, states in _FILTER_STATES.items():
        unknown = states - ALL_STATES
        assert not unknown, f"筛选「{name}」引用了不存在的状态: {sorted(unknown)}"


# === 主桶构成一个划分（无遗漏、无重复）===


def test_main_buckets_cover_every_state():
    """一个状态都不许落在三个主桶之外。

    落在外面的行只有「全部」能看到，而「全部」在几千条历史里 —— 等于不可达。
    `cancelled` 在旧的 7 Tab 设计里就是这么丢的。
    """
    covered = frozenset().union(*(_FILTER_STATES[b] for b in SORTING_BUCKETS))
    assert ALL_STATES - covered == frozenset(), f"无处可去的状态: {sorted(ALL_STATES - covered)}"


def test_main_buckets_are_pairwise_disjoint():
    """重叠会让「全部」小于三个主桶之和，用户直接看出数字对不上。"""
    for i, left in enumerate(SORTING_BUCKETS):
        for right in SORTING_BUCKETS[i + 1 :]:
            overlap = _FILTER_STATES[left] & _FILTER_STATES[right]
            assert not overlap, f"「{left}」和「{right}」同时收下: {sorted(overlap)}"


def test_main_buckets_introduce_no_extra_states():
    """反向闭合：主桶里也不许出现 `tasks.state` 之外的状态。"""
    covered = frozenset().union(*(_FILTER_STATES[b] for b in SORTING_BUCKETS))
    assert covered - ALL_STATES == frozenset()


def test_previously_unreachable_states_now_have_a_home():
    """回归钉子：这三个是精简前「有状态没入口」或「有入口没文案」的。"""
    # 旧设计里根本没有 Tab，只能在「全部」里翻
    assert "cancelled" in _FILTER_STATES["failed"]
    # 系统替你按的暂停，任务一个都没下完 —— 归「下载中」，不是无主状态
    assert "quality_guard" in _FILTER_STATES["active"]
    # DB 独有的中间态：没有 worker 的快照行直接读该列，漏掉就会从「下载中」消失
    assert {"downloading", "parsing"} <= _FILTER_STATES["active"]


# === 二级筛选：只做细分，不引入新状态 ===


def test_every_secondary_filter_narrows_a_main_bucket():
    """二级筛选必须是某个主桶的子集 —— 它是「细看」，不是第五个桶。"""
    for name in _SECONDARY_FILTERS:
        states = _FILTER_STATES[name]
        assert states, f"二级筛选「{name}」没有状态约束，会等同于「全部」"
        assert any(states <= _FILTER_STATES[b] for b in SORTING_BUCKETS), (
            f"二级筛选「{name}」跨了多个主桶: {sorted(states)}"
        )


def test_missing_filter_narrows_completed():
    """「文件丢失」是 completed 的子集，额外条件（`file_exists is False`）在 proxy 里加。"""
    assert _FILTER_STATES["missing"] == _FILTER_STATES["completed"]


# === 计数域必须跟着主桶走 ===


def test_count_domains_are_derived_from_the_main_buckets():
    """终态问 DB、非终态问模型；两个域正是主桶的重组，不是另抄的字面量。"""
    assert _MEMORY_COUNT_STATES == _FILTER_STATES["active"]
    assert _DB_COUNT_STATES == _FILTER_STATES["completed"] | _FILTER_STATES["failed"]


def test_count_domains_partition_all_states():
    """两个来源不相交且合起来是全集 —— 这是「绝不相加」之外的另一半保证：
    不相交所以不会重复计数，是全集所以不会漏掉整整一类。"""
    assert _MEMORY_COUNT_STATES & _DB_COUNT_STATES == frozenset()
    assert _MEMORY_COUNT_STATES | _DB_COUNT_STATES == ALL_STATES


def test_db_count_domain_matches_storage_terminal_states():
    """DB 侧的计数域就是 storage 层声明的终态集合。

    分工的依据是**行来源的所有权**（终态归 `fetchMore()` 分页、未完成态归
    `load_unfinished_tasks()` 注入），所以这两处定义漂移了，计数和列表内容就会脱节。
    """
    assert _DB_COUNT_STATES == frozenset(TERMINAL_STATES)
    assert _MEMORY_COUNT_STATES == frozenset(UNFINISHED_STATES)
