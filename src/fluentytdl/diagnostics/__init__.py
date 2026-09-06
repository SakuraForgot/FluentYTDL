"""错误诊断体系：规则表驱动的 yt-dlp 输出分类。

五层职责：

- ``models``  —— 数据模型（``Diagnosis`` / ``DiagnosticEvent`` / ``RetryPolicy``），不依赖 Qt
- ``rules``   —— 加载 ``assets/error_rules.json`` 与用户覆盖层，产出 ``RuleSet``
- ``engine``  —— 逐行解析 → 事件流 → 主因仲裁 → ``Diagnosis``
- ``collect`` —— "哪些原始行值得喂给引擎"的唯一定义（下载 / 提取 / 解析三路共用）
- ``catalog`` —— 错误码 → 本地化文案（唯一需要 Qt 的模块，且为惰性导入）

调用方只需 ``from ..diagnostics import diagnose``，其余细节不必关心。
"""

from __future__ import annotations

from .catalog import (
    describe,
    generic_suggestion,
    generic_unknown_title,
    hint_for,
    known_codes,
    localize,
)
from .collect import (
    DEFAULT_MAXLEN,
    DiagnosticLineCollector,
    extract_diagnostic_lines,
    is_diagnostic_line,
)
from .engine import diagnose, parse_events, pick_primary, strip_ansi
from .models import (
    FALLBACK_CODE,
    NEVER_RETRY,
    SUBTITLE_WARNING_CODES,
    VALID_CATEGORIES,
    VALID_RETRY_KINDS,
    VALID_SEVERITIES,
    Diagnosis,
    DiagnosticEvent,
    RetryPolicy,
)
from .rules import RuleSet, get_rule_set, load_rule_set, reload_rule_set

__all__ = [
    "DEFAULT_MAXLEN",
    "FALLBACK_CODE",
    "NEVER_RETRY",
    "SUBTITLE_WARNING_CODES",
    "VALID_CATEGORIES",
    "VALID_RETRY_KINDS",
    "VALID_SEVERITIES",
    "DiagnosticEvent",
    "DiagnosticLineCollector",
    "Diagnosis",
    "RetryPolicy",
    "RuleSet",
    "describe",
    "diagnose",
    "extract_diagnostic_lines",
    "generic_suggestion",
    "generic_unknown_title",
    "get_rule_set",
    "hint_for",
    "is_diagnostic_line",
    "known_codes",
    "load_rule_set",
    "localize",
    "parse_events",
    "pick_primary",
    "reload_rule_set",
    "strip_ansi",
]
