"""Render built-in notifications in the current language without rewriting history."""

import re
import string

from PySide6.QtCore import QT_TRANSLATE_NOOP

from ..utils.message_catalog import catalog
from ..utils.ui_text import DisplayText, tr_text

# Only built-in templates are eligible for legacy matching. Announcement content
# and arbitrary user text must never be guessed or translated by substring.
_LEGACY_SOURCES = (
    QT_TRANSLATE_NOOP("RuntimeText", "新版本"),
    QT_TRANSLATE_NOOP("RuntimeText", "预发布版本"),
    QT_TRANSLATE_NOOP("RuntimeText", "FluentYTDL {0} {1}"),
    QT_TRANSLATE_NOOP("RuntimeText", "{0} 有新版本 {1}"),
    QT_TRANSLATE_NOOP("RuntimeText", "{0} 需要切换频道"),
    QT_TRANSLATE_NOOP("RuntimeText", "{0} {1} 已可用，前往「设置 → 系统 → 关于」即可更新。"),
    QT_TRANSLATE_NOOP("RuntimeText", "{0} {1} 已可用（当前 {2}），前往「设置 → 组件」即可更新。"),
    QT_TRANSLATE_NOOP(
        "RuntimeText",
        "{0} 将从 {1} 频道切换到 {2} 频道（{3} → {4}），前往「设置 → 组件」即可应用。",
    ),
    QT_TRANSLATE_NOOP(
        "RuntimeText",
        "\n当前使用的是系统 PATH 上的版本，更新会在应用自带目录下安装一份并优先使用。",
    ),
    QT_TRANSLATE_NOOP("RuntimeText", "队列已自动暂停"),
    QT_TRANSLATE_NOOP(
        "RuntimeText",
        "连续 {0} 个任务画质未达标，已暂停排队中的 {1} 个任务。请检查源视频的可用格式、目标分辨率和访问权限后再继续。",
    ),
)


_CURRENT_SOURCES = {
    _LEGACY_SOURCES[5]: QT_TRANSLATE_NOOP(
        "RuntimeText", "{0} {1} 已可用，前往「设置 → 更新」即可更新。"
    ),
    _LEGACY_SOURCES[6]: QT_TRANSLATE_NOOP(
        "RuntimeText", "{0} {1} 已可用（当前 {2}），前往「设置 → 更新」即可更新。"
    ),
    _LEGACY_SOURCES[7]: QT_TRANSLATE_NOOP(
        "RuntimeText",
        "{0} 将从 {1} 频道切换到 {2} 频道（{3} → {4}），前往「设置 → 更新」即可应用。",
    ),
}


def _encode(value):
    if isinstance(value, DisplayText):
        return {
            "source": value.message_source,
            "args": [_encode(arg) for arg in value.message_args],
            "kwargs": {key: _encode(arg) for key, arg in value.message_kwargs.items()},
        }
    return value


def capture_fields(notification) -> dict:
    if notification.type == "announcement":
        return {}
    return {
        field: _encode(value)
        for field in ("title", "message")
        if isinstance(value := getattr(notification, field), DisplayText)
    }


def _decode(value, depth=0):
    if depth > 8:
        raise ValueError("Notification template nesting is too deep")
    if not isinstance(value, dict):
        return value
    return tr_text(
        value["source"],
        *[_decode(arg, depth + 1) for arg in value["args"]],
        **{key: _decode(arg, depth + 1) for key, arg in value["kwargs"].items()},
    )


def _legacy(value: str) -> str:
    from ..utils.control_center_text import MESSAGES, text

    for key, pair in MESSAGES.items():
        if value in pair:
            return text(key)
    for kind in _LEGACY_SOURCES[:2]:
        for label in (kind, catalog().get(kind, kind)):
            prefix = f"FluentYTDL {label} "
            if value.startswith(prefix):
                return str(tr_text("FluentYTDL {0} {1}", tr_text(kind), value[len(prefix) :]))
    for source in (*_LEGACY_SOURCES, *_CURRENT_SOURCES.values()):
        for template in (source, catalog().get(source, source)):
            parts, fields = [], []
            for literal, field, _, _ in string.Formatter().parse(template):
                parts.append(re.escape(literal.strip("\n")))
                if field is not None:
                    fields.append(int(field))
                    if field == "0" and source in (
                        _LEGACY_SOURCES[5],
                        _CURRENT_SOURCES[_LEGACY_SOURCES[5]],
                    ):
                        labels = [
                            label
                            for kind in _LEGACY_SOURCES[:2]
                            for label in (kind, catalog().get(kind, kind))
                        ]
                        parts.append("(" + "|".join(re.escape(label) for label in labels) + ")")
                    else:
                        parts.append("(.+?)")
            match = re.fullmatch("".join(parts), value)
            if match is None:
                continue
            args = [""] * (max(fields, default=-1) + 1)
            for field, arg in zip(fields, match.groups(), strict=True):
                # Only the app's release-kind argument is another localized label.
                args[field] = (
                    _legacy(arg)
                    if (
                        source.startswith("FluentYTDL")
                        or source in (_LEGACY_SOURCES[5], _CURRENT_SOURCES[_LEGACY_SOURCES[5]])
                    )
                    and field == 0
                    else arg
                )
            current_source = _CURRENT_SOURCES.get(source, source)
            return str(tr_text(current_source, *args)).strip("\n")
    return value


def display_fields(notification) -> tuple[str, str]:
    if notification.type == "announcement":
        return notification.title, notification.message
    metadata = notification.metadata if isinstance(notification.metadata, dict) else {}
    saved = metadata.get("display_text", {})
    result = []
    for field in ("title", "message"):
        raw = getattr(notification, field)
        try:
            value = _decode(saved[field]) if field in saved else _decode(_encode(raw))
            result.append("\n".join(_legacy(line) for line in str(value).split("\n")))
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            result.append(str(raw))
    return tuple(result)
