"""Isolated bilingual strings while the translation task owns shared catalogs."""

from __future__ import annotations

MESSAGES = {
    "offline_notice": (
        "当前网络不可用，显示的是 24 小时内缓存的公告。",
        "Offline: showing an announcement cached within the last 24 hours.",
    ),
    "update_timeout": (
        "更新请求超时，自动重试后仍未成功。请检查网络后重试。",
        "The update request timed out after retries. Check your connection and try again.",
    ),
    "update_network": (
        "无法连接更新服务，自动重试后仍未成功。请检查网络和代理设置。",
        "Could not connect to the update service after retries. Check your connection and proxy settings.",
    ),
    "update_certificate": (
        "更新连接的 SSL 证书验证失败。请检查系统时间、可信证书和代理设置。",
        "The update connection failed SSL certificate verification. Check your system clock, trusted certificates and proxy settings.",
    ),
    "update_tls": (
        "更新连接的 TLS 握手失败，请检查网络或代理后重试。",
        "The update connection failed its TLS handshake. Check your connection or proxy and try again.",
    ),
    "update_rate_limited": (
        "更新请求受到限流，请稍后重试。",
        "Update requests are rate limited. Please try again later.",
    ),
    "update_service": (
        "更新服务暂时不可用，请稍后重试。",
        "The update service is temporarily unavailable. Please try again later.",
    ),
    "update_http": (
        "更新服务器拒绝请求或文件不存在，请查看日志。",
        "The update server rejected the request or the file is unavailable. See the logs for details.",
    ),
    "update_invalid_response": (
        "更新服务返回了无效数据，请稍后重试。",
        "The update service returned invalid data. Please try again later.",
    ),
    "update_cancelled": ("更新请求已取消。", "The update request was cancelled."),
    "update_sources_failed": (
        "本次检查已尝试两个更新源，均未成功。请稍后重试。",
        "Both update sources failed during this check. Please try again later.",
    ),
    "update_cache_expired": (
        "更新信息缓存已过期，暂时无法确认最新版本。",
        "The cached update information has expired. The latest version cannot be confirmed.",
    ),
    "update_truncated": (
        "更新文件下载不完整，自动重试后仍未成功。",
        "The update download is incomplete after retries.",
    ),
    "update_hash": (
        "更新文件校验失败，已停止安装。请重新下载。",
        "Update file verification failed. Installation was stopped. Please download it again.",
    ),
    "source_title": ("更新检查源", "Update check source"),
    "source_description": (
        "用于软件与组件版本检查；文件始终通过 GitHub 直链下载。GitHub 限流时仅本次切换 Cloudflare。",
        "Used for app and component checks. Files download directly from GitHub. GitHub rate limits trigger a one-time switch to Cloudflare.",
    ),
    "github": ("GitHub 直连", "GitHub direct"),
    "cloudflare": ("Cloudflare", "Cloudflare"),
    "announcements_refreshed": ("公告已刷新。", "Announcements refreshed."),
    "announcements": ("公告", "Announcements"),
    "refresh_announcements": ("刷新公告", "Refresh announcements"),
    "acknowledge": ("我已知晓", "I understand"),
    "close": ("关闭", "Close"),
    "details": ("查看详情", "View details"),
    "inactive": (
        "此公告已过期或下线，仅供查阅。",
        "This announcement has expired or been withdrawn and is shown for reference.",
    ),
    "check_failed": ("更新检查未完成", "Update checks could not complete"),
    "automatic_checks_failed": (
        "部分自动更新检查未完成，请在设置页重试。详细原因已记录到日志。",
        "Some automatic update checks could not complete. Retry in Settings; details are recorded in the logs.",
    ),
    "retrying": ("正在自动重试（第 {0}/3 次）…", "Retrying automatically (attempt {0}/3)…"),
}


def text(key: str, *args) -> str:
    import locale

    from .language import normalize_language

    try:
        from ..core.config_manager import config_manager

        language = config_manager.get("app_language")
    except Exception:
        language = "auto"
    english = (
        normalize_language(str(language or "auto"), locale.getlocale()[0] or "en_US") == "en_US"
    )
    pair = MESSAGES.get(key, (key, key))
    return pair[int(english)].format(*args)
