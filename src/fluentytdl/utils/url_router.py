"""
FluentYTDL 链接路由器

职责:
1. 平台检测 (YouTube / X / Unknown)
2. URL 规范化 (vxtwitter→x.com, mobile→标准, t.co 展开, 清理跟踪参数)
3. 链接类型分类 (tweet_video / profile / spaces / ...)
4. 准入判定 (是否允许继续解析)
5. Cookie 平台路由

设计原则:
- 纯函数，无状态，线程安全
- 不依赖 yt-dlp，在 yt-dlp 调用前完成所有预处理
- t.co 展开是唯一需要网络的操作，通过 _expand_tco_url 隔离
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

import requests
from PySide6.QtCore import QT_TRANSLATE_NOOP, QObject, QThread, Signal

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text

from ..core.config_manager import config_manager
from ..utils.logger import logger


@dataclass
class UrlProcessResult:
    original_url: str  # 用户原始输入
    normalized_url: str  # 规范化后的 URL
    platform: str  # "youtube" | "twitter" | "unknown"
    link_type: str  # 内部类型标识
    link_type_display: str  # 用户可见的类型名称 (可翻译)
    accepted: bool  # 是否允许继续
    rejection_reason: str | None  # 拒绝原因
    cookie_platform: str | None  # Cookie 平台标识 ("youtube" | "twitter")


class _AsyncExpandWorker(QThread):
    finished_signal = Signal(UrlProcessResult)

    def __init__(self, url: str, router: UrlRouter):
        super().__init__()
        self.url = url
        self.router = router

    def run(self):
        result = self.router.process(self.url)
        self.finished_signal.emit(result)


class UrlRouter:
    """链接路由器"""

    # === X 平台正则 ===
    # 唯一放行：推文视频
    _X_STATUS_RE = re.compile(
        r"^https?://(?:(?:www|mobile)\.)?(?:twitter\.com|x\.com)/"
        r"(?:\w+|i(?:/web)?)/(?:status|statuses)/(\d+)",
        re.IGNORECASE,
    )

    # 明确拒绝的类型 (按优先级排列)
    _X_REJECT_PATTERNS = [
        (
            re.compile(r"/i/spaces/\w+", re.I),
            "spaces",
            QT_TRANSLATE_NOOP("RuntimeText", "X Spaces 暂不支持下载"),
        ),
        (
            re.compile(r"/i/lists/\d+", re.I),
            "list",
            QT_TRANSLATE_NOOP("RuntimeText", "X 列表暂不支持下载"),
        ),
        (
            re.compile(r"/i/communities/", re.I),
            "community",
            QT_TRANSLATE_NOOP("RuntimeText", "X 社区暂不支持下载"),
        ),
        (
            re.compile(r"/i/bookmarks", re.I),
            "bookmarks",
            QT_TRANSLATE_NOOP("RuntimeText", "X 书签暂不支持下载"),
        ),
        (
            re.compile(r"/i/moments/", re.I),
            "moments",
            QT_TRANSLATE_NOOP("RuntimeText", "X Moments 暂不支持下载"),
        ),
        (
            re.compile(r"/i/grok", re.I),
            "grok",
            QT_TRANSLATE_NOOP("RuntimeText", "此链接非内容页面"),
        ),
        (
            re.compile(r"/search\?", re.I),
            "search",
            QT_TRANSLATE_NOOP("RuntimeText", "X 搜索结果暂不支持下载"),
        ),
        (
            re.compile(r"/explore", re.I),
            "explore",
            QT_TRANSLATE_NOOP("RuntimeText", "X 探索页暂不支持下载"),
        ),
        (
            re.compile(r"/hashtag/", re.I),
            "hashtag",
            QT_TRANSLATE_NOOP("RuntimeText", "X 话题标签暂不支持下载"),
        ),
        (
            re.compile(r"/notifications", re.I),
            "notifications",
            QT_TRANSLATE_NOOP("RuntimeText", "此链接非内容页面"),
        ),
        (
            re.compile(r"/messages", re.I),
            "messages",
            QT_TRANSLATE_NOOP("RuntimeText", "此链接非内容页面"),
        ),
        (
            re.compile(r"/settings/", re.I),
            "settings",
            QT_TRANSLATE_NOOP("RuntimeText", "此链接非内容页面"),
        ),
        (
            re.compile(r"/(?:following|followers|verified_followers)", re.I),
            "social",
            QT_TRANSLATE_NOOP("RuntimeText", "此链接非内容页面"),
        ),
        # 用户主页/媒体页 (最后匹配，因为路径最短)
        (
            re.compile(
                r"^https?://(?:(?:www|mobile)\.)?(?:twitter\.com|x\.com)/(\w+)/?(media|likes|with_replies)?/?$",
                re.I,
            ),
            "profile",
            QT_TRANSLATE_NOOP("RuntimeText", "X 平台用户主页暂不支持下载，请粘贴具体推文链接"),
        ),
    ]

    # 镜像域名
    _MIRROR_DOMAINS = {
        "vxtwitter.com": "x.com",
        "fxtwitter.com": "x.com",
        "fixvx.com": "x.com",
        "twittpr.com": "x.com",
        "nitter.net": "x.com",
    }

    # X 平台跟踪参数 (清理)
    _X_TRACKING_PARAMS = {"s", "t", "ref_src", "ref_url", "src"}

    @classmethod
    def detect_platform(cls, url: str) -> str:
        """快速检测平台"""
        url = url.lower()
        if "youtube.com" in url or "youtu.be" in url:
            return "youtube"
        if (
            "twitter.com" in url
            or "x.com" in url
            or "t.co" in url
            or any(d in url for d in cls._MIRROR_DOMAINS)
        ):
            return "twitter"
        return "unknown"

    @classmethod
    def fast_check(cls, url: str) -> str:
        """快速无阻塞地判断 URL 类型（供 UI 防抖和输入框提示使用）"""
        url = url.strip()
        if not url:
            return "unknown"

        lower_url = url.lower()
        if "youtube.com" in lower_url or "youtu.be" in lower_url:
            if "list=" in lower_url:
                return "youtube_playlist"
            return "youtube_video"

        if lower_url.startswith("https://t.co/") or lower_url.startswith("http://t.co/"):
            return "tco_shortlink"

        if (
            "twitter.com" in lower_url
            or "x.com" in lower_url
            or any(d in lower_url for d in cls._MIRROR_DOMAINS)
        ):
            if cls._X_STATUS_RE.search(url):
                return "twitter_video"
            return "twitter_unsupported"

        return "unknown"

    def process(self, url: str) -> UrlProcessResult:
        """处理 URL，返回预处理结果"""
        original_url = url.strip()
        url = original_url

        if not url:
            return UrlProcessResult(
                original_url=original_url,
                normalized_url="",
                platform="unknown",
                link_type="unknown",
                link_type_display=tr_text("未知链接"),
                accepted=False,
                rejection_reason=tr_text("链接不能为空"),
                cookie_platform=None,
            )

        # 1. t.co 展开
        if url.startswith("https://t.co/") or url.startswith("http://t.co/"):
            expanded = self._expand_tco_url(url)
            if not expanded:
                return UrlProcessResult(
                    original_url=original_url,
                    normalized_url=url,
                    platform="twitter",
                    link_type="t.co",
                    link_type_display=tr_text("t.co 短链接"),
                    accepted=False,
                    rejection_reason=tr_text("短链接展开失败，请手动在浏览器打开复制完整链接"),
                    cookie_platform=None,
                )
            url = expanded

        # 2. 平台检测
        platform = self.detect_platform(url)

        if platform == "twitter":
            return self._process_twitter(original_url, url)
        elif platform == "youtube":
            return self._process_youtube(original_url, url)
        else:
            return UrlProcessResult(
                original_url=original_url,
                normalized_url=url,
                platform="unknown",
                link_type="unknown",
                link_type_display=tr_text("未知链接"),
                accepted=False,
                rejection_reason=tr_text("不支持的平台"),
                cookie_platform=None,
            )

    def process_async(
        self, url: str, callback: Callable[[UrlProcessResult], None], parent: QObject | None = None
    ) -> None:
        """异步处理，主要用于 UI 层避免 t.co 展开阻塞"""
        self._worker = _AsyncExpandWorker(url, self)
        if parent:
            self._worker.setParent(parent)
        self._worker.finished_signal.connect(callback)
        self._worker.finished_signal.connect(self._worker.deleteLater)
        self._worker.start()

    def _process_twitter(self, original_url: str, url: str) -> UrlProcessResult:
        """处理 X 平台链接"""
        # A. 规范化: 域名替换
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()

        # 移除 mobile.
        if netloc.startswith("mobile."):
            netloc = netloc[7:]

        # 替换镜像域名
        for mirror, target in self._MIRROR_DOMAINS.items():
            if netloc == mirror:
                netloc = target
                break

        # 统一使用 x.com (yt-dlp 两个都支持，统一一下比较干净，这里如果保留也可以，目前默认替换为 x.com 方便)
        if netloc == "twitter.com":
            netloc = "x.com"

        # B. 规范化: 清理跟踪参数
        query = urllib.parse.parse_qs(parsed.query)
        cleaned_query = {k: v for k, v in query.items() if k not in self._X_TRACKING_PARAMS}
        new_query = urllib.parse.urlencode(cleaned_query, doseq=True)

        normalized_url = urllib.parse.urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, new_query, parsed.fragment)
        )

        # C. 分类与拒绝
        for pattern, link_type, reason in self._X_REJECT_PATTERNS:
            if pattern.search(normalized_url):
                return UrlProcessResult(
                    original_url=original_url,
                    normalized_url=normalized_url,
                    platform="twitter",
                    link_type=link_type,
                    link_type_display=tr_text("不支持的链接"),
                    accepted=False,
                    rejection_reason=tr_text(reason),
                    cookie_platform="twitter",
                )

        # 检查是否为支持的推文视频
        if self._X_STATUS_RE.search(normalized_url):
            return UrlProcessResult(
                original_url=original_url,
                normalized_url=normalized_url,
                platform="twitter",
                link_type="tweet_video",
                link_type_display=tr_text("𝕏 推文视频"),
                accepted=True,
                rejection_reason=None,
                cookie_platform="twitter",
            )

        # 默认拒绝
        return UrlProcessResult(
            original_url=original_url,
            normalized_url=normalized_url,
            platform="twitter",
            link_type="unknown",
            link_type_display=tr_text("未知页面"),
            accepted=False,
            rejection_reason=tr_text("无法识别该 X 平台链接类型"),
            cookie_platform="twitter",
        )

    def _process_youtube(self, original_url: str, url: str) -> UrlProcessResult:
        """处理 YouTube 链接 (由于项目原本支持良好，此处仅作放行)"""
        link_type = "video"
        display = tr_text("🎬 YouTube 视频")

        if "playlist" in url.lower():
            link_type = "playlist"
            display = tr_text("🎬 YouTube 播放列表")
        elif "@" in url or "/channel/" in url or "/c/" in url or "/user/" in url:
            link_type = "channel"
            display = tr_text("🎬 YouTube 频道")
        elif "/shorts/" in url.lower():
            link_type = "shorts"
            display = tr_text("🎬 YouTube 短视频")

        return UrlProcessResult(
            original_url=original_url,
            normalized_url=url,
            platform="youtube",
            link_type=link_type,
            link_type_display=display,
            accepted=True,
            rejection_reason=None,
            cookie_platform="youtube",
        )

    def _expand_tco_url(self, short_url: str, timeout: float = 3.0) -> str | None:
        """展开 t.co 短链接"""
        proxies = self._get_proxies_from_config()

        try:
            resp = requests.head(
                short_url,
                allow_redirects=True,
                timeout=timeout,
                proxies=proxies,
                headers={"User-Agent": "FluentYTDL/1.0"},
            )
            return resp.url
        except (requests.Timeout, requests.ConnectionError, requests.RequestException) as e:
            log_text(logger, "warning", "[UrlRouter] t.co 展开失败: {0}", e)
            return None

    def _get_proxies_from_config(self) -> dict | None:
        """从 config_manager 获取代理配置"""
        proxy_mode = config_manager.get("proxy_mode", "off")
        proxy_url = str(config_manager.get("proxy_url", "") or "").strip()

        if proxy_mode not in ("http", "socks5") or not proxy_url:
            return None

        lower = proxy_url.lower()
        if (
            lower.startswith("http://")
            or lower.startswith("https://")
            or lower.startswith("socks5://")
        ):
            full_url = proxy_url
        else:
            scheme = "socks5" if proxy_mode == "socks5" else "http"
            full_url = f"{scheme}://{proxy_url}"

        return {"http": full_url, "https": full_url}


# 实例化全局单例方便调用
url_router = UrlRouter()
