"""诊断体系的数据模型。

本模块刻意不导入 PySide6：引擎与规则表可以在无 Qt 环境下单测。
文案（user_title / user_message / recovery_hint）通过惰性导入 catalog 取得，
这样语言切换后重新读取属性即可拿到新语言的文案。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

Level = Literal["error", "warning"]
Severity = Literal["fatal", "recoverable", "warning"]
RetryKind = Literal["never", "immediate", "backoff", "after_fix"]

#: 允许出现在规则表 category 字段里的值。
VALID_CATEGORIES = frozenset(
    {
        "auth",  # 登录 / Cookie / 会员 / 年龄限制 / 机器人验证
        "network",  # 连接 / DNS / 代理 / SSL / 限流
        "media",  # 视频状态 / 格式 / 地区限制 / 版权
        "filesystem",  # 磁盘 / 权限 / 文件名
        "toolchain",  # yt-dlp / FFmpeg / POT provider 等组件问题
        "unknown",  # 兜底
    }
)

VALID_SEVERITIES = frozenset({"fatal", "recoverable", "warning"})
VALID_RETRY_KINDS = frozenset({"never", "immediate", "backoff", "after_fix"})

#: 兜底错误码，任何未命中规则的错误都归到这里，保证 100% 有归属。
FALLBACK_CODE = "unknown"


def as_severity(value: str) -> Severity:
    """把规则表 / 序列化数据里的裸字符串收窄成 ``Severity``。

    规则表是外部 JSON，字段类型只能是 ``str``；这里做一次运行时校验并收窄，
    非法值一律降级成 ``fatal``（宁可多提示一次，也不要把致命错误标成警告）。
    """
    return cast(Severity, value) if value in VALID_SEVERITIES else "fatal"


@dataclass(frozen=True)
class DiagnosticEvent:
    """yt-dlp 输出中的单行诊断事件。"""

    code: str
    level: Level
    component: str  # "youtube" | "ffmpeg" | "pot" | ""，无组件时为空串
    raw_line: str
    line_no: int
    priority: int = 0

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "level": self.level,
            "component": self.component,
            "raw_line": self.raw_line,
            "line_no": self.line_no,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DiagnosticEvent:
        level = data.get("level", "error")
        return cls(
            code=str(data.get("code", FALLBACK_CODE)),
            level="warning" if level == "warning" else "error",
            component=str(data.get("component", "")),
            raw_line=str(data.get("raw_line", "")),
            line_no=int(data.get("line_no", 0) or 0),
            priority=int(data.get("priority", 0) or 0),
        )


@dataclass(frozen=True)
class RetryPolicy:
    """规则驱动的重试策略。

    - never:     直接失败，不挂起、不重试（视频已删除、URL 不支持等）
    - immediate: 立即重试，无退避（瞬时中断）
    - backoff:   指数退避重试 base_sec * 2**attempt（限流、连接超时）
    - after_fix: 挂起等待用户修复后再重试（Cookie、POT 类）
    """

    policy: RetryKind = "after_fix"
    max_attempts: int = 0
    base_sec: int = 0

    @property
    def is_automatic(self) -> bool:
        """是否可以自动重试（无需用户介入）。"""
        return self.policy in ("immediate", "backoff") and self.max_attempts > 0

    def delay_for(self, attempt: int) -> float:
        """第 attempt 次重试前应等待的秒数（attempt 从 0 起）。"""
        if self.policy != "backoff":
            return 0.0
        return float(self.base_sec) * (2**attempt)

    def to_dict(self) -> dict:
        return {
            "policy": self.policy,
            "max_attempts": self.max_attempts,
            "base_sec": self.base_sec,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> RetryPolicy:
        if not isinstance(data, dict):
            return cls()
        policy = str(data.get("policy", "after_fix"))
        if policy not in VALID_RETRY_KINDS:
            policy = "after_fix"
        # 规则表里用 "max"，反序列化 to_dict() 结果时用 "max_attempts"，两者都认。
        raw_max = data.get("max_attempts", data.get("max", 0))
        try:
            max_attempts = int(raw_max or 0)
        except (TypeError, ValueError):
            max_attempts = 0
        try:
            base_sec = int(data.get("base_sec", 0) or 0)
        except (TypeError, ValueError):
            base_sec = 0
        return cls(policy=policy, max_attempts=max(0, max_attempts), base_sec=max(0, base_sec))


NEVER_RETRY = RetryPolicy(policy="never")


@dataclass
class Diagnosis:
    """一次 yt-dlp 失败的完整诊断结果。

    `code` 是仲裁出的主因；`events` 保留全量事件流，伴随信号（例如主因是 403
    但事件流里有 nsig 提取失败）用于文案增强与 fix_action 覆盖。
    """

    code: str = FALLBACK_CODE
    category: str = "unknown"
    severity: Severity = "fatal"
    component: str = ""
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    fix_action: str | None = None
    events: list[DiagnosticEvent] = field(default_factory=list)
    exit_code: int = 1
    raw_tail: str = ""
    #: 引擎追加的补充说明（组件过旧、nsig 提取失败等伴随信号），已本地化。
    extra_notes: list[str] = field(default_factory=list)
    #: 兜底路径下引擎直接给出的文案，非空时优先于 catalog。
    override_title: str = ""
    override_message: str = ""

    # ---- 文案（惰性取自 catalog，语言切换后自动跟随）----

    @property
    def user_title(self) -> str:
        if self.override_title:
            return self.override_title
        from .catalog import describe

        return describe(self.code)[0]

    @property
    def user_message(self) -> str:
        from .catalog import describe

        base = self.override_message or describe(self.code)[1]
        if self.extra_notes:
            return base + "\n" + "\n".join(self.extra_notes)
        return base
    @property
    def recovery_hint(self) -> str:
        from .catalog import hint_for

        return hint_for(self.fix_action)

    @property
    def technical_detail(self) -> str:
        return f"exit_code={self.exit_code}\n{self.raw_tail}"

    def has_event(self, code: str) -> bool:
        """事件流中是否出现过某个码（用于伴随信号判断）。"""
        return any(ev.code == code for ev in self.events)

    def to_dict(self) -> dict:
        """序列化以便通过 Qt Signal 传递。键名保持 UI 侧既有读法。"""
        return {
            "code": self.code,
            "category": self.category,
            "severity": self.severity,
            "component": self.component,
            "retry": self.retry.to_dict(),
            "fix_action": self.fix_action,
            "events": [ev.to_dict() for ev in self.events],
            "exit_code": self.exit_code,
            "raw_tail": self.raw_tail,
            "extra_notes": list(self.extra_notes),
            "override_title": self.override_title,
            "override_message": self.override_message,
            # 展开后的文案，供不想再走 catalog 的消费方直接读取
            "user_title": self.user_title,
            "user_message": self.user_message,
            "recovery_hint": self.recovery_hint,
            "technical_detail": self.technical_detail,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Diagnosis:
        severity = as_severity(str(data.get("severity", "fatal")))
        category = data.get("category", "unknown")
        if category not in VALID_CATEGORIES:
            category = "unknown"
        raw_events = data.get("events") or []
        return cls(
            code=str(data.get("code", FALLBACK_CODE)),
            category=category,
            severity=severity,
            component=str(data.get("component", "")),
            retry=RetryPolicy.from_dict(data.get("retry")),
            fix_action=data.get("fix_action"),
            events=[DiagnosticEvent.from_dict(e) for e in raw_events if isinstance(e, dict)],
            exit_code=int(data.get("exit_code", 1) or 1),
            raw_tail=str(data.get("raw_tail", "")),
            extra_notes=[str(n) for n in (data.get("extra_notes") or [])],
            override_title=str(data.get("override_title", "")),
            override_message=str(data.get("override_message", "")),
        )
