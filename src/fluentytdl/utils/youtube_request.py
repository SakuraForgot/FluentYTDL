"""Internal request metadata; never serialized to yt-dlp arguments."""

from contextvars import ContextVar

COOKIE_MODE = "__fluentytdl_youtube_cookies_enabled"
SABR_SCOPE = "__fluentytdl_youtube_sabr_scope"
request_scope: ContextVar[dict | None] = ContextVar("youtube_request", default=None)


def enforce_cookie_mode(opts: dict) -> None:
    if opts.get(COOKIE_MODE) is False:
        opts.pop("cookiefile", None)
        opts.pop("cookiesfrombrowser", None)
        opts.pop("cookies_from_browser", None)
        opts[SABR_SCOPE] = ""


def stamp_result(info: dict, enabled: bool) -> None:
    info[COOKIE_MODE] = enabled
    for entry in info.get("entries") or ():
        if isinstance(entry, dict):
            stamp_result(entry, enabled)
