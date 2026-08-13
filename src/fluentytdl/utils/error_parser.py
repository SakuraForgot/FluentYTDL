"""错误诊断的兼容适配层。

真正的分类逻辑住在 :mod:`fluentytdl.diagnostics` —— 规则表驱动、可单测、
文案走 Qt 翻译。本模块只保留两样东西：

1. ``diagnose_error()`` —— 历史签名的入口，转调 ``diagnostics.diagnose``
2. 与分类无关的两个工具函数（连通性探测、Issue 链接生成）

新代码请直接 ``from ..diagnostics import diagnose``。
"""

from typing import Any

from ..diagnostics import Diagnosis, diagnose


def diagnose_error(
    exit_code: int, stderr: str, parsed_json: dict[str, Any] | None = None
) -> Diagnosis:
    """核心诊断函数：根据退出码、错误输出和 JSON 快照，生成诊断对象。"""
    return diagnose(exit_code, stderr, parsed_json)


def probe_youtube_connectivity(timeout: float = 5.0) -> bool:
    """
    HEAD 请求 youtube.com 检测网络连通性（不经过 yt-dlp）。
    会自动读取应用内代理配置。
    """
    import urllib.request

    try:
        from ..core.config_manager import config_manager

        proxy_mode = str(config_manager.get("proxy_mode") or "off").lower().strip()
        proxy_url = str(config_manager.get("proxy_url", "") or "").strip()
    except Exception:
        proxy_mode = "off"
        proxy_url = ""

    try:
        req = urllib.request.Request(
            "https://www.youtube.com/",
            method="HEAD",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        handlers: list = []
        if proxy_mode == "manual" and proxy_url:
            lower = proxy_url.lower()
            if not (
                lower.startswith("http://")
                or lower.startswith("https://")
                or lower.startswith("socks5://")
            ):
                proxy_url = "http://" + proxy_url
            handlers.append(urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url}))
        elif proxy_mode == "system":
            pass
        else:
            handlers.append(urllib.request.ProxyHandler({}))

        opener = urllib.request.build_opener(*handlers)
        resp = opener.open(req, timeout=timeout)
        return resp.status < 400
    except Exception:
        return False


def generate_issue_url(title: str, raw_error: str) -> str:
    """生成预填内容的 GitHub Issue 链接"""
    import urllib.parse

    max_err_len = 1500
    if len(raw_error) > max_err_len:
        raw_error = raw_error[:max_err_len] + "\n...[Truncated]"
    issue_title = urllib.parse.quote(f"[AutoReport] {title}")
    body = f"### 错误描述\n自动捕获到的错误：\n**{title}**\n\n### 错误日志\n```text\n{raw_error}\n```\n\n### 其他信息\n- FluentYTDL 版本: \n- 操作系统: \n"
    issue_body = urllib.parse.quote(body)
    return f"https://github.com/SakuraForgot/FluentYTDL/issues/new?title={issue_title}&body={issue_body}&labels=bug"
