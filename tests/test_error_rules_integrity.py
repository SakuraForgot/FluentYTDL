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


def test_regex_patterns_compile() -> None:
    """加载器对坏正则是静默跳过的，这里显式断言一条都没被跳过。"""
    declared = sum(len(r.get("patterns", [])) for r in _raw_rules())
    loaded = sum(len(r.patterns) for r in RULES)
    assert loaded == declared, f"有 {declared - loaded} 条模式在加载时被丢弃（正则错误？）"


def test_declared_rule_count_matches_loaded() -> None:
    assert len(RULES) == len(_raw_rules()), "有规则在加载时被丢弃（字段缺失？）"
