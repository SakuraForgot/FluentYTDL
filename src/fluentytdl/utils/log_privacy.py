"""Shared, bounded redaction for diagnostic records and exported text."""

from __future__ import annotations

import math
import re
from pathlib import Path

POLICY_VERSION = 1
_USERINFO = re.compile(r"(?<=://)[^/@\s]+@")
_HOME = re.compile(r"([/\\])(?:Users|home)[/\\][^/\\\s\"']+", re.I)
_KEY = r"(?:cookie|cookies|password|passwd|secret|(?:access_|refresh_|auth_|po_?)?token|api[_-]?key|authorization|ct0|signature|sig|lsig|x-goog-signature|x-amz-signature)"
_PAIR = re.compile(
    rf"(\b{_KEY}\b[\"']?\s*[:=]\s*)(?:Bearer\s+|Basic\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s;,&#\"']+)",
    re.I,
)
_HEADER = re.compile(r"(\b(?:cookie|set-cookie|authorization)\s*:\s*)[^\r\n]+", re.I)
_SECRET_KEY = re.compile(
    rf"^{_KEY}$|^(?:cookie|cookies|headers|.*[_-](?:token|password|secret|api_key))$", re.I
)


def redact_text(value: object, limit: int | None = None) -> str:
    text = str(value)
    text = _USERINFO.sub("", text)
    text = _HEADER.sub(r"\1***", text)
    text = _PAIR.sub(r"\1***", text)
    try:
        home = str(Path.home())
        text = re.sub(re.escape(home), "~", text, flags=re.I)
    except Exception:
        pass
    text = _HOME.sub(lambda m: m.group(1) + "Users" + m.group(1) + "<user>", text)
    return text if limit is None or len(text) <= limit else text[:limit] + "…"


def redact_value(value, depth: int = 0):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if depth >= 8:
        return "<depth-limit>"
    if isinstance(value, dict):
        return {
            str(k): "***"
            if _SECRET_KEY.match(str(k)) and not isinstance(v, bool)
            else redact_value(v, depth + 1)
            for k, v in list(value.items())[:256]
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_value(v, depth + 1) for v in list(value)[:256]]
    return redact_text(value, 16000)
