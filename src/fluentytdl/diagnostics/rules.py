"""规则表加载与合并。

规则数据来自两处：
1. 打包内 ``assets/error_rules.json``（随发行版分发，只读）
2. 可选的用户覆盖层 ``<user_data_dir>/error_rules.override.json``

覆盖层按 ``code`` 合并：同 code 整条替换，新 code 追加。任何加载失败都只记
warning 并回退到内置规则 —— 诊断系统本身绝不能成为崩溃源。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..utils.logger import logger
from .models import (
    VALID_CATEGORIES,
    VALID_SEVERITIES,
    Level,
    RetryPolicy,
    Severity,
    as_severity,
)

OVERRIDE_FILENAME = "error_rules.override.json"


@dataclass(frozen=True)
class LoadedPattern:
    kind: str  # "substr" | "regex"
    needle: str  # substr: 已小写化；regex: 原始串
    regex: re.Pattern[str] | None = None

    def matches(self, line: str, line_lower: str) -> bool:
        if self.regex is not None:
            return bool(self.regex.search(line))
        return self.needle in line_lower


@dataclass(frozen=True)
class LoadedRule:
    code: str
    category: str
    severity: Severity
    applies_to: str  # "error" | "warning" | "both"
    priority: int
    retry: RetryPolicy
    fix_action: str | None
    component: str  # 空串表示不限定组件
    patterns: tuple[LoadedPattern, ...]

    def applies_to_level(self, level: Level) -> bool:
        if self.applies_to == "both":
            return True
        return self.applies_to == level

    def matches_component(self, component: str) -> bool:
        if not self.component:
            return True
        return component.lower() == self.component

    def find_match(self, line: str, line_lower: str) -> bool:
        return any(p.matches(line, line_lower) for p in self.patterns)


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[LoadedRule, ...]
    fallback_code: str
    version: int
    source_paths: tuple[str, ...]

    def by_code(self, code: str) -> LoadedRule | None:
        for r in self.rules:
            if r.code == code:
                return r
        return None


def _packaged_rules_path() -> Path:
    from ..utils.paths import resource_path

    return resource_path("assets", "error_rules.json")


def _override_rules_path() -> Path:
    from ..utils.paths import user_data_dir

    return user_data_dir() / OVERRIDE_FILENAME


def _parse_pattern(raw: Any, code: str) -> LoadedPattern | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind", "substr")).lower()
    value = raw.get("value")
    if not isinstance(value, str) or not value:
        logger.warning("[diagnostics] 规则 {} 的 pattern 缺少 value，已跳过", code)
        return None

    if kind == "regex":
        try:
            compiled = re.compile(value, re.IGNORECASE)
        except re.error as exc:
            logger.warning("[diagnostics] 规则 {} 的正则无效（{}），已跳过: {}", code, exc, value)
            return None
        return LoadedPattern(kind="regex", needle=value, regex=compiled)

    if kind != "substr":
        logger.warning("[diagnostics] 规则 {} 的 pattern kind 未知: {}", code, kind)
        return None
    return LoadedPattern(kind="substr", needle=value.lower())


def _parse_rule(raw: Any) -> LoadedRule | None:
    if not isinstance(raw, dict):
        return None
    code = raw.get("code")
    if not isinstance(code, str) or not code:
        logger.warning("[diagnostics] 发现缺少 code 的规则，已跳过")
        return None

    patterns = tuple(
        p for p in (_parse_pattern(x, code) for x in (raw.get("patterns") or [])) if p is not None
    )
    if not patterns:
        logger.warning("[diagnostics] 规则 {} 没有任何有效 pattern，已跳过", code)
        return None

    category = str(raw.get("category", "unknown"))
    if category not in VALID_CATEGORIES:
        logger.warning("[diagnostics] 规则 {} 的 category 非法（{}），归为 unknown", code, category)
        category = "unknown"

    severity_raw = str(raw.get("severity", "fatal"))
    if severity_raw not in VALID_SEVERITIES:
        logger.warning(
            "[diagnostics] 规则 {} 的 severity 非法（{}），归为 fatal", code, severity_raw
        )
    severity = as_severity(severity_raw)

    applies_to = str(raw.get("appliesTo", "both")).lower()
    if applies_to not in ("error", "warning", "both"):
        applies_to = "both"

    try:
        priority = int(raw.get("priority", 0) or 0)
    except (TypeError, ValueError):
        priority = 0

    fix_action = raw.get("fix")
    if fix_action is not None and not isinstance(fix_action, str):
        fix_action = None

    return LoadedRule(
        code=code,
        category=category,
        severity=severity,
        applies_to=applies_to,
        priority=priority,
        retry=RetryPolicy.from_dict(raw.get("retry")),
        fix_action=fix_action or None,
        component=str(raw.get("component", "") or "").lower(),
        patterns=patterns,
    )


def _read_json(path: Path) -> dict | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[diagnostics] 规则文件读取失败，已忽略: {} ({})", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("[diagnostics] 规则文件顶层不是对象，已忽略: {}", path)
        return None
    return data


def _merge(base: list[LoadedRule], extra: list[LoadedRule]) -> list[LoadedRule]:
    """按 code 合并：同 code 整条替换（保持原位置），新 code 追加。"""
    index = {r.code: i for i, r in enumerate(base)}
    merged = list(base)
    for rule in extra:
        pos = index.get(rule.code)
        if pos is None:
            index[rule.code] = len(merged)
            merged.append(rule)
        else:
            merged[pos] = rule
    return merged


def load_rule_set(
    packaged_path: Path | None = None, override_path: Path | None = None
) -> RuleSet:
    """加载并合并规则表。任何一层失败都不会抛异常。"""
    packaged_path = packaged_path or _packaged_rules_path()
    override_path = override_path or _override_rules_path()

    sources: list[str] = []
    fallback_code = "unknown"
    version = 0
    rules: list[LoadedRule] = []

    packaged = _read_json(packaged_path)
    if packaged is not None:
        rules = [r for r in (_parse_rule(x) for x in (packaged.get("rules") or [])) if r]
        fallback_code = str(packaged.get("fallbackCode", fallback_code))
        try:
            version = int(packaged.get("version", 0) or 0)
        except (TypeError, ValueError):
            version = 0
        sources.append(str(packaged_path))

    if not rules:
        # 内置规则缺失是部署事故（打包漏了 assets），但不能让下载流程崩掉：
        # 引擎会退化成纯兜底（HTTP 码 + extractor 名提取），仍然可用。
        logger.warning("[diagnostics] 未加载到任何内置规则，诊断将退化为兜底模式: {}", packaged_path)

    override = _read_json(override_path)
    if override is not None:
        extra = [r for r in (_parse_rule(x) for x in (override.get("rules") or [])) if r]
        if extra:
            rules = _merge(rules, extra)
            sources.append(str(override_path))
            logger.info("[diagnostics] 已应用规则覆盖层，{} 条: {}", len(extra), override_path)
        if isinstance(override.get("fallbackCode"), str):
            fallback_code = override["fallbackCode"]

    # 排序只影响同优先级时的稳定性；主因仲裁另有三级规则，见 engine.py
    rules.sort(key=lambda r: -r.priority)

    return RuleSet(
        rules=tuple(rules),
        fallback_code=fallback_code,
        version=version,
        source_paths=tuple(sources),
    )


_lock = threading.Lock()
_cached: RuleSet | None = None


def get_rule_set() -> RuleSet:
    """进程内单例。规则表是只读数据，加载一次即可。"""
    global _cached
    if _cached is None:
        with _lock:
            if _cached is None:
                _cached = load_rule_set()
    return _cached


def reload_rule_set() -> RuleSet:
    """强制重新加载（改过覆盖层后调用，也供测试用）。"""
    global _cached
    with _lock:
        _cached = load_rule_set()
    return _cached
