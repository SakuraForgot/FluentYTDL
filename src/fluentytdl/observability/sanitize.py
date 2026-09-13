"""日志脱敏：把凭据、用户名、绝对路径从**要落盘的**文本里摘掉。

为什么必须有这一层：日志文件是用户会直接贴到 GitHub Issue 里的东西，而
`executor.py` 那句 `logger.info("cmd={}", " ".join(cmd))` 原样打印整条 argv ——
里面可能有 `socks5://user:pass@host`、cookie 文件路径、含 Windows 用户名的完整
输出路径。`--extractor-args` 里还可能带 PO Token（`log_pot_in_argv` 的注释写得很
明确：只记 base_url，绝不记 Token）。

原则：**够用就好，不做无谓的删减。** 视频 URL、文件名这些本来就显示在界面上、
也已经出现在 yt-dlp 自己的 `[download] Destination:` 行里的信息一律保留 ——
把它们也涂掉只会让日志没法用来排查，换不到任何安全收益。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..utils.log_privacy import redact_text

MASK = "***"

#: 单条字段的长度上限。日志行进 UI 查看器，也进 JSONL；一条 argv 或异常消息
#: 动辄几千字符，截断比让查看器卡住好。
MAX_FIELD_CHARS = 2000


def _home() -> str:
    try:
        return str(Path.home())
    except Exception:
        return ""


_HOME = _home()
#: 兜底：`Path.home()` 拿不到时（服务账户、容器），仍然把 Users/<name> 折掉。
_USER_DIR_RE = re.compile(r"([/\\])(Users|home)\1[^/\\]+", re.IGNORECASE)
#: URL 里的 userinfo（`scheme://user:pass@host`）。
_USERINFO_RE = re.compile(r"(?<=://)[^/@\s]+:[^/@\s]*@")


def _truncate(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def sanitize_path(path: str | os.PathLike[str] | None) -> str:
    """折掉用户主目录：``C:\\Users\\alice\\Videos\\a.mp4`` → ``~\\Videos\\a.mp4``。

    路径本身对排查很有用（哪个盘、哪个子目录、什么文件名），泄漏的只有用户名 ——
    所以只折前缀，不折结构。
    """
    if path is None:
        return ""
    text = str(path)
    if not text:
        return ""
    if _HOME and text.lower().startswith(_HOME.lower()):
        text = "~" + text[len(_HOME) :]
    else:
        text = _USER_DIR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}<user>", text)
    return _truncate(text)


def sanitize_url(url: str | None) -> str:
    """摘掉 URL 里的 userinfo，其余原样。

    视频 URL 保留：它是排查的起点，且早已遍布日志与界面。
    """
    if not url:
        return ""
    return redact_text(_USERINFO_RE.sub("", str(url)), MAX_FIELD_CHARS)


def sanitize_proxy(url: str | None) -> str:
    """代理地址只留 ``scheme://host:port``，账号密码一律丢弃。"""
    if not url:
        return ""
    text = str(url)
    try:
        parts = urlsplit(text)
        if not parts.hostname:
            # 不是标准 URL（可能是 `host:port`），退回正则摘 userinfo
            return _truncate(_USERINFO_RE.sub("", text))
        netloc = parts.hostname
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, "", "", ""))
    except Exception:
        return "<proxy>"


def sanitize_exception(exc: BaseException | None) -> str:
    """``TypeError: 消息``，其中的绝对路径与 URL 凭据一并处理。

    异常消息是路径泄漏的重灾区（``[Errno 2] ... 'C:\\Users\\alice\\...'``）。
    """
    if exc is None:
        return ""
    try:
        text = f"{type(exc).__name__}: {exc}"
    except Exception:
        text = type(exc).__name__
    return redact_text(sanitize_path(_USERINFO_RE.sub("", text)), MAX_FIELD_CHARS)


# ── argv ────────────────────────────────────────────────────

#: 值必须整体涂掉的开关。
_MASK_VALUE_FLAGS = frozenset(
    {
        "--username",
        "-u",
        "--password",
        "-p",
        "--videopassword",
        "--ap-username",
        "--ap-password",
        "--twofactor",
        "-2",
        "--client-certificate-password",
        "--netrc-cmd",
    }
)
#: 值是代理地址的开关。
_PROXY_FLAGS = frozenset({"--proxy", "--geo-verification-proxy", "--cn-verification-proxy"})
#: 值是路径、折主目录即可的开关。
_PATH_FLAGS = frozenset(
    {
        "-o",
        "--output",
        "-P",
        "--paths",
        "--cache-dir",
        "--ffmpeg-location",
        "--load-info-json",
        "--batch-file",
        "-a",
        "--download-archive",
        "--home",
        "--temp",
    }
)
#: cookie 文件按计划只留文件名 —— 路径对排查的价值不如"用的是哪一份"。
_BASENAME_FLAGS = frozenset({"--cookies", "--netrc-location", "--client-certificate"})

#: `--extractor-args` 里的敏感键。POT 只允许记 base_url。
_ARG_SECRET_RE = re.compile(
    r"\b(po_?token|token|auth|secret|password|api_?key)=([^;\s]+)", re.IGNORECASE
)
#: `--add-header Authorization: Bearer xxx`。
_HEADER_SECRET_RE = re.compile(
    r"^(authorization|cookie|x-api-key|x-goog-api-key)\s*:.*$", re.IGNORECASE
)


def mask_secrets(text: str | None) -> str:
    """把 ``key=value`` 形式里的凭据涂掉（``po_token=xxx`` → ``po_token=***``）。

    给"不是完整 argv、但内容一样敏感"的片段用。典型是 ``--extractor-args`` 的值
    ``youtube:po_token=<用户的完整 Token>;player_client=tv`` —— 它在拼进 argv **之前**
    就被单独打过日志，绕过了 `sanitize_argv()`。
    """
    if not text:
        return ""
    return _truncate(_ARG_SECRET_RE.sub(lambda m: f"{m.group(1)}={MASK}", str(text)))


def _sanitize_value(flag: str, value: str) -> str:
    if flag in _MASK_VALUE_FLAGS:
        return MASK
    if flag in _PROXY_FLAGS:
        return sanitize_proxy(value)
    if flag in _BASENAME_FLAGS:
        return os.path.basename(value) or MASK
    if flag in _PATH_FLAGS:
        return sanitize_path(value)
    if flag == "--add-header":
        if _HEADER_SECRET_RE.match(value.strip()):
            name = value.split(":", 1)[0]
            return f"{name}:{MASK}"
        return value
    return mask_secrets(value)


def sanitize_argv(argv: Sequence[Any] | None) -> list[str]:
    """整条命令行脱敏，返回可安全落盘的副本。

    未列名的开关也会走一遍 `_ARG_SECRET_RE`：新增一个带 token 的
    `--extractor-args` 变体时不至于直接漏出去。
    """
    if not argv:
        return []
    out: list[str] = []
    pending_flag = ""
    for raw in argv:
        item = str(raw)
        if pending_flag:
            out.append(_sanitize_value(pending_flag, item))
            pending_flag = ""
            continue
        if item.startswith("-"):
            # `--flag=value` 形式（yt-dlp 两种都接受）
            if "=" in item:
                flag, _, value = item.partition("=")
                out.append(f"{flag}={_sanitize_value(flag, value)}")
                continue
            out.append(item)
            if (
                item in _MASK_VALUE_FLAGS
                or item in _PROXY_FLAGS
                or item in _PATH_FLAGS
                or item in _BASENAME_FLAGS
                or item == "--add-header"
            ):
                pending_flag = item
            continue
        # 位置参数：可能是 URL，也可能是被上一个未列名开关吃掉的值
        out.append(mask_secrets(sanitize_url(item)))
    return out


def render_argv(argv: Sequence[Any] | None) -> str:
    """脱敏后拼成一行，供 ``logger.info("cmd={}", render_argv(cmd))`` 直接用。"""
    return _truncate(" ".join(sanitize_argv(argv)))
