"""规则表自检：assets/error_rules.json 与 catalog 的一致性。

规则表是数据，数据错了引擎不会报错，只会静默地把错误分错类。这些断言就是
那道防线：code 唯一、字段合法、priority 不打架、每条 code 都有文案。
"""

import sys
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.diagnostics import (  # noqa: E402
    FALLBACK_CODE,
    SUBTITLE_WARNING_CODES,
    VALID_CATEGORIES,
    VALID_RETRY_KINDS,
    VALID_SEVERITIES,
    get_rule_set,
    known_codes,
)

RULES = get_rule_set().rules


def _raw_rules() -> list[dict]:
    """直接读打包内的规则 JSON —— 和加载器读的是同一个文件。

    存在用户覆盖层时跳过：那时 ``RULES`` 里混了覆盖条目，条数对不上是正常的。
    """
    import json

    from fluentytdl.diagnostics.rules import _override_rules_path, _packaged_rules_path

    if _override_rules_path().exists():
        pytest.skip("检测到用户覆盖规则文件，跳过条数比对")
    return json.loads(Path(_packaged_rules_path()).read_text("utf-8"))["rules"]


def test_rule_set_is_not_empty() -> None:
    assert len(RULES) > 0


def test_codes_are_unique() -> None:
    codes = [r.code for r in RULES]
    dupes = {c for c in codes if codes.count(c) > 1}
    assert not dupes, f"重复的错误码: {sorted(dupes)}"


def test_rules_are_sorted_by_priority_desc() -> None:
    """引擎靠"取第一条命中"实现优先级，所以加载后必须是降序。"""
    priorities = [r.priority for r in RULES]
    assert priorities == sorted(priorities, reverse=True)


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.code)
def test_rule_fields_are_valid(rule) -> None:
    assert rule.category in VALID_CATEGORIES, f"{rule.code}: 非法 category {rule.category!r}"
    assert rule.severity in VALID_SEVERITIES, f"{rule.code}: 非法 severity {rule.severity!r}"
    assert rule.retry.policy in VALID_RETRY_KINDS, f"{rule.code}: 非法 retry.policy"
    assert rule.patterns, f"{rule.code}: 没有任何匹配模式，永远不会命中"
    assert rule.applies_to in ("error", "warning", "both"), f"{rule.code}: 非法 appliesTo"


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.code)
def test_automatic_retry_has_a_budget(rule) -> None:
    """声明了自动重试却没给 max，等于永远不重试 —— 多半是写规则时漏了。"""
    if rule.retry.policy in ("immediate", "backoff"):
        assert rule.retry.max_attempts > 0, f"{rule.code}: {rule.retry.policy} 但 max=0"
    if rule.retry.policy == "backoff":
        assert rule.retry.base_sec > 0, f"{rule.code}: backoff 但 base_sec=0"


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.code)
def test_warning_rules_are_not_fatal(rule) -> None:
    if rule.applies_to == "warning":
        assert rule.severity != "fatal", f"{rule.code}: warning 行不该是 fatal"


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.code)
def test_every_rule_code_has_catalog_text(rule) -> None:
    """漏登记文案的 code 会在 UI 上退化成兜底文案，用户看不懂。"""
    assert rule.code in known_codes(), f"{rule.code}: catalog.py 里没有对应文案"


def test_fallback_code_has_catalog_text() -> None:
    assert FALLBACK_CODE in known_codes()


# ── bareLine schema ────────────────────────────────────────────────


def test_bare_line_flag_round_trips_from_json() -> None:
    """``bareLine`` 是新加的 schema 字段，得确认它真的从 JSON 读进来了。

    漏读的后果很隐蔽：加载不报错、规则照常存在，只是 `_match_bare_line` 永远
    找不到候选，那句 `[info] There are no subtitles…` 又变回无声消失。
    """
    declared = {r["code"] for r in _raw_rules() if r.get("bareLine")}
    loaded = {r.code for r in RULES if r.bare_line}

    assert declared, "规则表里没有任何 bareLine 规则，无级别前缀的行将无人接管"
    assert loaded == declared


@pytest.mark.parametrize("rule", [r for r in RULES if r.bare_line], ids=lambda r: r.code)
def test_bare_line_rules_are_specific_enough(rule) -> None:
    """裸行匹配不看级别前缀，进度行、文件名都会流过它。

    因此 bareLine 规则的模式必须是长到不可能偶然出现的整句；顺带钉住"裸行只产
    warning 事件"这个前提 —— 声明成 fatal 就会让一行 `[info]` 判死整个任务。
    """
    assert rule.severity == "warning", f"{rule.code}: 裸行规则只能是 warning"
    for pattern in rule.patterns:
        assert pattern.kind == "substr", f"{rule.code}: 裸行规则请用 substr，避免正则误伤"
        assert len(pattern.needle) >= 20, f"{rule.code}: 模式 {pattern.needle!r} 太短，容易误命中"


# ── demoteToWarning schema ─────────────────────────────────────────


def test_demote_flag_round_trips_from_json() -> None:
    """和 `bareLine` 一样是新加的 schema 字段，漏读同样是静默失效。

    漏读的后果：规则照常加载，字幕子请求的 `ERROR:` 行照常命中 p98/p97，然后带着这个
    高 priority 进入 error 层，把真正的失败原因挤成配角。
    """
    declared = {r["code"] for r in _raw_rules() if r.get("demoteToWarning")}
    loaded = {r.code for r in RULES if r.demote_to_warning}

    assert declared, "没有任何 demoteToWarning 规则，字幕子请求的 ERROR: 行会挤掉真正的主因"
    assert loaded == declared


@pytest.mark.parametrize("rule", [r for r in RULES if r.demote_to_warning], ids=lambda r: r.code)
def test_demoted_rules_are_warning_severity_and_reach_error_lines(rule) -> None:
    """降级规则的两条硬约束。

    - ``appliesTo`` 不能是 ``"warning"``：那样 `ERROR:` 行连匹配的机会都没有，降级永远
      不会发生 —— 这正是修复前的状态，真机上那句 `ERROR: Unable to download video
      subtitles ... 429` 一路落到泛化的 `rate_limited_429`；
    - ``severity`` 必须是 warning：降级后 `_is_warning_only_primary` 靠 severity 判定要不
      要在 rc != 0 时放弃这个主因，声明成 fatal 会把护栏直接拆掉。
    """
    assert rule.applies_to != "warning", f"{rule.code}: 只收 warning 行的话降级形同虚设"
    assert rule.severity == "warning", f"{rule.code}: 降级规则不能是 {rule.severity}"


# ── 成功后扫描的三码 ────────────────────────────────────────────────


@pytest.mark.parametrize("code", sorted(SUBTITLE_WARNING_CODES))
def test_subtitle_scan_codes_are_warning_and_never_retry(code: str) -> None:
    """`workers._scan_subtitle_warnings` 在**任务已经成功**之后才跑这几个码。

    两条硬约束：``severity`` 必须是 warning（视频已经在磁盘上了，标红是误报），
    ``retry`` 必须是 never（少一个 `.vtt` 不该让整个任务重下一遍）。
    """
    rule = next((r for r in RULES if r.code == code), None)
    assert rule is not None, f"{code}: 扫描清单里的码在规则表里不存在"
    assert rule.severity == "warning", f"{code}: 成功任务的提示不能是 {rule.severity}"
    assert rule.retry.policy == "never", f"{code}: 字幕层失败不得触发整任务重试"


def test_regex_patterns_compile() -> None:
    """加载器对坏正则是静默跳过的，这里显式断言一条都没被跳过。"""
    declared = sum(len(r.get("patterns", [])) for r in _raw_rules())
    loaded = sum(len(r.patterns) for r in RULES)
    assert loaded == declared, f"有 {declared - loaded} 条模式在加载时被丢弃（正则错误？）"


def test_declared_rule_count_matches_loaded() -> None:
    assert len(RULES) == len(_raw_rules()), "有规则在加载时被丢弃（字段缺失？）"
