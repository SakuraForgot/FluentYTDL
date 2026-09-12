from __future__ import annotations

import asyncio
import copy
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import requests

from fluentytdl.utils.logger import get_logger
from fluentytdl.utils.paths import find_bundled_executable, is_frozen, locate_runtime_tool

from ..core.config_manager import config_manager
from .yt_dlp_cli import YtDlpCancelled, run_dump_single_json, run_version

LogCallback = Callable[[str, str], None]


def _short_url_tag(url: str) -> str:
    """日志用的短标识：优先取 YouTube video id，否则取末段路径。仅用于 grep 定位。"""
    if not url:
        return "-"
    try:
        parsed = urlparse(url)
        vid = parse_qs(parsed.query).get("v", [""])[0]
        if vid:
            return vid
        tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        return tail or parsed.netloc or "-"
    except Exception:
        return "-"


def _resolve_preferred_audio_langs() -> list[str]:
    """用户的音轨语言偏好，规范化 + 去重后按优先级排列。

    走 `bcp47.canonicalize()` 而不是原样透传：设置页允许自定义标签，用户手打的
    `zh-hans` / `PT-br` 要和目录里的 `zh-Hans` / `pt-BR` 落到同一个写法上，
    下游的别名展开才认得。`orig` / `original` 在这里被剔除 —— 原音是**策略**
    （`_fytdl_audio_strategy`），不是语言列表里的一个条目（Phase A 已迁移）。
    """
    from ..utils import bcp47

    raw = config_manager.get("preferred_audio_languages")
    if not isinstance(raw, list):
        return []

    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text or text.lower() in {"orig", "original"}:
            continue
        if not bcp47.is_safe_tag(text):
            # 非法字符会跑进格式过滤器里，那是命令行的一部分 —— 直接丢
            continue
        canon = bcp47.canonicalize(text)
        if canon not in out:
            out.append(canon)
    return out


def _resolve_audio_strategy() -> str:
    """音轨策略键，非法值回落 `original_first`（与 `DEFAULT_CONFIG` 一致）。"""
    from ..utils.format_scorer import AUDIO_STRATEGIES, STRATEGY_ORIGINAL_FIRST

    value = str(config_manager.get("audio_track_strategy") or "").strip()
    return value if value in AUDIO_STRATEGIES else STRATEGY_ORIGINAL_FIRST


@dataclass(slots=True)
class YtDlpAuthOptions:
    """Authentication inputs.

    Priority: cookies_file (直接指定) > AuthService (统一管理).
    """

    cookies_file: str | None = None


@dataclass(slots=True)
class AntiBlockingOptions:
    """Anti-blocking / anti-bot options."""

    player_clients: tuple[str, ...] = ("android", "ios", "web")
    # sleep_interval 已禁用：裸 yt-dlp 测试证实其对单视频下载有害，
    # 会导致 YouTube 下载 URL 签名在传输中过期 → 403。
    sleep_interval_min: int = 0
    sleep_interval_max: int = 0


@dataclass(slots=True)
class NetworkOptions:
    proxy: str | None = None  # http/socks5
    socket_timeout: int = 15
    retries: int = 10
    fragment_retries: int = 10


@dataclass(slots=True)
class YoutubeServiceOptions:
    auth: YtDlpAuthOptions = field(default_factory=YtDlpAuthOptions)
    anti_blocking: AntiBlockingOptions = field(default_factory=AntiBlockingOptions)
    network: NetworkOptions = field(default_factory=NetworkOptions)


class YoutubeService:
    """Singleton service that wraps yt-dlp calls.

    Stage 1 scope:
    - Cookies injection (cookies.txt and cookies-from-browser)
    - Anti-blocking (random UA, mobile client simulation, random sleep)
    - Proxy
    - Async API via asyncio.to_thread (UI thread must never block)
    """

    _instance: YoutubeService | None = None
    _lock = threading.Lock()

    # 解析结果缓存容量上限（info_dict 不小，必须有界）。
    # 按 mode 分桶而非共享一个总额：播放列表逐条深解析（entry_detail）动辄上百条，
    # 若与弹窗结果共用一个 LRU，爬一次大列表就会把 dialog / playlist_flat 全挤掉，
    # P2 的收益反被 P2.1 吃掉。分桶后一个 mode 只能淘汰自己桶里的旧条目。
    _PARSE_CACHE_MAX = 32  # 未列入下表的 mode 的兜底上限
    _PARSE_CACHE_LIMITS: dict[str, int] = {
        "dialog": 12,
        "playlist_flat": 8,
        "vr": 12,
        "entry_detail": 64,
        "channel_tab": 9,  # 3 个频道 × 3 个标签页
    }

    # 单条 info 的 entries 上限：超过就不写缓存。
    # 频道 / 大播放列表的 flat dump 可能有数千条，_parse_cache_get 的 deepcopy 是实打实的
    # 成本（几十 MB 常驻 + 每次命中几百毫秒），此时缓存反而变成负收益。
    _PARSE_CACHE_MAX_ENTRIES = 3000

    def __new__(cls) -> YoutubeService:
        if cls._instance is not None:
            return cls._instance
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._logger = get_logger("fluentytdl.YoutubeService")
        self._log_callback: LogCallback | None = None

        # === 弹窗解析结果 TTL 缓存（P2）===
        # 只服务"解析 → 弹窗"路径；下载路径绝不读取，否则会拿到过期签名 URL 触发 403。
        # 不落盘：签名 URL 有时效，持久化只会带来假命中。
        self._parse_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._parse_cache_lock = threading.Lock()

    def set_log_callback(self, callback: LogCallback | None) -> None:
        """UI layer can subscribe to logs later (Stage 2+)."""

        self._log_callback = callback

    def _emit_log(self, level: str, message: str) -> None:
        if self._log_callback is not None:
            try:
                self._log_callback(level, message)
            except Exception:
                # Never let UI callback break core logic
                pass
        getattr(self._logger, level.lower(), self._logger.info)(message)

    @staticmethod
    def _is_page_reload_error(message: str) -> bool:
        text = (message or "").lower()
        return "the page needs to be reloaded" in text

    def _try_refresh_cookie_for_reload_error(self, url: str = "") -> bool:
        """For WebView2 mode, force-refresh cookie once to recover transient session mismatch.

        只刷这条链接对应的平台。不带 platform 调用时，`force_refresh_with_uac()` 会同时
        占用两个平台并在 WebView2 模式下弹出**两个**登录窗 —— 解析一条 X 链接失败，
        用户却要先把 YouTube 也登录一遍。
        """
        try:
            from ..auth.auth_service import AuthSourceType, auth_service
            from ..auth.cookie_sentinel import cookie_sentinel
            from ..utils.url_router import UrlRouter

            if auth_service.current_source != AuthSourceType.WEBVIEW2:
                return False

            platform = "twitter" if UrlRouter.detect_platform(url) == "twitter" else "youtube"

            self._emit_log(
                "warning",
                "检测到 'The page needs to be reloaded'，正在自动刷新 WebView2 Cookie 并重试一次...",
            )
            ok, msg = cookie_sentinel.force_refresh_with_uac(platform=platform)
            if ok:
                self._emit_log("info", "自动刷新 WebView2 Cookie 成功，准备重试解析")
                return True
            self._emit_log("warning", f"自动刷新 WebView2 Cookie 失败: {msg}")
            return False
        except Exception as e:
            self._emit_log("warning", f"自动刷新 WebView2 Cookie 异常: {e}")
            return False

    def build_ydl_options(
        self, options: YoutubeServiceOptions | None = None, *, url: str = ""
    ) -> dict[str, Any]:
        """Construct yt-dlp options with anti-blocking and auth."""

        options = options or YoutubeServiceOptions()

        # 平台检测
        from ..utils.url_router import UrlRouter

        _platform = UrlRouter.detect_platform(url) if url else "youtube"
        _is_twitter = _platform == "twitter"

        # --- Global config (SettingsPage) ---
        download_dir = str(config_manager.get("download_dir"))
        if download_dir:
            try:
                Path(download_dir).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

        auth = options.auth
        net = options.network

        # 应用全局网络重试设置
        network_retries = int(config_manager.get("network_retries", 10))
        if net.retries == 10:
            net.retries = network_retries
        if net.fragment_retries == 10:
            net.fragment_retries = network_retries

        ydl_opts: dict[str, Any] = {
            # `quiet` / `no_warnings` 曾经在这里，是**死配置**：`ydl_opts_to_cli_args()`
            # 里没有它们的映射，全项目也没有一处 `YoutubeDL(...)` 会读这个 dict，
            # 所以它们从未变成过命令行参数。删掉，免得下一个读代码的人以为
            # "解析路径是静默的" —— 解析路径本来就看得见警告，P0-B 的 `signal` 靠的就是它。
            # For single-video parsing we must NOT ignore errors; otherwise yt-dlp may return None/False
            # and we lose the real failure reason (e.g. cookies required).
            "ignoreerrors": False,
            # Anti-blocking
            # Anti-blocking
            # NOTE: Do NOT force youtube "player_client" simulation via extractor_args.
            # Some videos may return an incomplete/empty format list under android/ios
            # simulation, causing "Requested format is not available".
            # Let yt-dlp choose the most stable default (web) extractor behavior.
            # NOTE: sleep_interval 已移除 — 裸 yt-dlp 测试证实其对单视频下载有害,
            # 会导致 YouTube 的带签名下载 URL 在传输过程中过期 → 触发 403.
            # Network
            "socket_timeout": int(net.socket_timeout),
            "retries": int(net.retries),
            "fragment_retries": int(net.fragment_retries),
            # Download output
            "outtmpl": "%(title)s.%(ext)s",
            "overwrites": True,  # 强制覆盖同名文件，防止因文件存在导致任务直接跳过并显示为完成
        }

        if download_dir:
            ydl_opts["paths"] = {"home": str(download_dir)}

        # 音轨语言偏好**不进 format_sort**。`-S lang:xx` 从来不表示"偏好某语言"：
        # `lang` 是 `language_preference` 的**数值**别名，喂它一个语言码会让 yt-dlp 把
        # 全局 `settings['lang']['convert']` 改成 `'string'`，拿 10/5/−1/−10 当字符串跟
        # `"ja"` 比；更要紧的是 `FormatSorter.add_item` 有 `if field in self._order: return`,
        # 所以十几个 `lang:` 条目里**只有第一个会被接受**。这整段展开的贡献是零。
        # 语言与原音偏好改由 `_inject_language_into_format()` 的格式过滤器表达
        # （`[language^=xx]` / `[language_preference>=?10]`），意图靠下面两个私有键传递。
        ydl_opts["format_sort"] = ["res", "br", "fps", "acodec"]

        # 下划线前缀 = 不进 argv。`ydl_opts_to_cli_args()` 是逐键显式映射而非遍历，
        # 所以没有映射的键天然到不了命令行。
        ydl_opts["_fytdl_audio_langs"] = _resolve_preferred_audio_langs()
        ydl_opts["_fytdl_audio_strategy"] = _resolve_audio_strategy()

        if not _is_twitter:
            self._maybe_configure_youtube_js_runtime(ydl_opts)

        # User-Agent: 不再自定义，让 yt-dlp 根据客户端类型自动处理
        # yt-dlp 会根据 extractor_args 中的 player_client 自动匹配合适的 UA

        # Proxy: options.network.proxy > SettingsPage proxy
        proxy_mode = str(config_manager.get("proxy_mode") or "off").lower().strip()
        proxy_url = str(config_manager.get("proxy_url") or "").strip()
        if net.proxy:
            ydl_opts["proxy"] = net.proxy
        elif proxy_mode == "off":
            # Important: On Windows, some environments may have system/ambient proxy settings.
            # If user selects OFF, explicitly disable proxies for yt-dlp.
            # Equivalent to CLI: --proxy ""
            ydl_opts["proxy"] = ""
        elif proxy_mode == "system":
            # system: do not override, allow ambient/system proxies
            pass
        else:
            # manual http/socks5
            if proxy_url:
                lower = proxy_url.lower()
                if (
                    lower.startswith("http://")
                    or lower.startswith("https://")
                    or lower.startswith("socks5://")
                ):
                    ydl_opts["proxy"] = proxy_url
                else:
                    scheme = "socks5" if proxy_mode == "socks5" else "http"
                    ydl_opts["proxy"] = f"{scheme}://{proxy_url}"
            else:
                ydl_opts["proxy"] = ""

        # Cookies: 统一通过 Cookie Sentinel 管理
        # 优先级: options.auth.cookies_file (直接指定) > Cookie Sentinel
        has_valid_cookie = False
        cookiefile = None

        # 1. 检查 auth options 中是否直接指定了 cookie 文件（向后兼容）
        direct_cookiefile = (auth.cookies_file or "").strip() or None

        if direct_cookiefile and os.path.exists(direct_cookiefile):
            # 直接指定的 cookie 文件优先
            if self._is_probably_json_cookie_file(direct_cookiefile):
                self._emit_log(
                    "error",
                    "Cookies 文件疑似为 JSON 格式，yt-dlp 只支持 Netscape HTTP Cookie File 格式；已忽略该文件。",
                )
            else:
                yt_cookie_count = self._count_youtube_related_cookies(direct_cookiefile)
                if yt_cookie_count <= 0:
                    self._emit_log(
                        "warning",
                        "已读取 cookies.txt，但未发现 YouTube/Google 域相关 cookies。"
                        "请确认是在 youtube.com 登录后导出，且为 Netscape 格式。",
                    )
                else:
                    cookiefile = direct_cookiefile
                    has_valid_cookie = True
                    self._emit_log(
                        "info",
                        f"✅ 已加载 Cookie 文件: {cookiefile} (YouTube/Google cookies: {yt_cookie_count})",
                    )
        else:
            # 2. 通过 Cookie Sentinel 获取统一的 bin/cookies.txt
            try:
                from ..auth.cookie_sentinel import cookie_sentinel

                cookie_target = "twitter" if _is_twitter else "youtube"
                sentinel_path = cookie_sentinel.get_cookie_path_for_platform(cookie_target)
                sentinel_cookie_file = str(sentinel_path)

                if sentinel_path.exists():
                    if _is_twitter:
                        yt_cookie_count = self._count_platform_cookies(
                            sentinel_cookie_file, "twitter"
                        )
                    else:
                        yt_cookie_count = self._count_youtube_related_cookies(sentinel_cookie_file)

                    if yt_cookie_count > 0:
                        cookiefile = sentinel_cookie_file
                        has_valid_cookie = True

                        # 显示状态信息 —— 全部按 cookie_target 取，
                        # 以前用的是 `age_minutes` / `is_stale` / `get_status_info()` 三个
                        # 无参属性，它们一律默认 youtube：解析 X 链接时日志会报 YouTube
                        # 真相源的年龄和来源，排查 X 问题的人被直接带偏。
                        age = cookie_sentinel.get_age_minutes(cookie_target)
                        age_str = f"{int(age)}分钟前" if age is not None else "未知"
                        status_emoji = "⚠️" if cookie_sentinel.get_is_stale(cookie_target) else "✅"
                        status_info = cookie_sentinel.get_status_info(cookie_target)

                        self._emit_log(
                            "info",
                            f"{status_emoji} Cookie Sentinel: {status_info['source']} "
                            f"(更新于 {age_str}, {yt_cookie_count} 个 {cookie_target.title()} Cookie)",
                        )

                        # 闸门刚拒过新 Cookie：当前用的是旧文件，下载失败时这行是唯一线索
                        if status_info.get("commit_warning"):
                            self._emit_log(
                                "warning",
                                f"⚠️ {cookie_target} 新 Cookie 未通过校验，仍在使用旧真相源: "
                                f"{status_info['commit_warning']}",
                            )
                    else:
                        self._emit_log(
                            "warning",
                            f"Cookie Sentinel 文件存在但未发现 {cookie_target.title()} 相关 Cookie",
                        )
                else:
                    self._emit_log(
                        "info",
                        "Cookie Sentinel 文件不存在，将使用无 Cookie 模式下载（可能受限）",
                    )

            except Exception as e:
                self._emit_log("warning", f"Cookie Sentinel 获取失败: {e}")

        # 设置 cookiefile 到 ydl_opts
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile

        self._emit_log(
            "debug",
            f"[Cookie] Path={cookiefile or 'None'}, Valid={has_valid_cookie}",
        )

        # --- Smart client switching ---
        # yt-dlp default strategy (tv -> web_safari -> android_vr) is the most robust.
        # We do not hardcode player_client to allow yt-dlp to adapt to YouTube SABR changes.
        if not has_valid_cookie:
            # 不指定 player_client，让 yt-dlp 使用默认策略
            # (内部会尝试 tv → web_safari → android_vr，自动选择可用的)
            self._emit_log("warning", "未检测到有效 Cookies，将使用无 Cookie 默认模式")
        else:
            # 不强制指定 player_client，让 yt-dlp 自行决定最优组合
            # yt-dlp 社区每天跟踪 YouTube 变化，default 是集体智慧的结晶
            self._emit_log("info", "🚀 Cookies 模式激活：交由 yt-dlp 自动管理最优客户端组合")

        # --- POT Provider 服务集成 ---
        # POT (Proof of Origin Token) Provider 提供动态 PO Token 生成服务
        # 类似 Cookie Sentinel 的策略：检测服务状态，自动注入 extractor_args
        # 硬约束：解析路径上绝不阻塞等待 POT。Token 由 POT 服务端缓存，等待并不会让
        # "这一次"解析更快，却会把最坏情况推到 35s（wait_until_ready 15s 超时后
        # 还会再跑一次 verify_token_generation 20s）。未预热时降级为无 POT 解析
        # （即当前默认关闭状态下的既有行为），同时触发一次后台预热。
        pot_injected = False
        _pot_url_tag = _short_url_tag(url)
        _pot_skip_reason = "twitter" if _is_twitter else "disabled"
        if not _is_twitter and config_manager.get("pot_provider_enabled", False):
            try:
                from .pot_manager import pot_manager

                if not pot_manager.is_running():
                    _pot_skip_reason = "not_running"
                    pot_manager.ensure_warm_async()
                elif not pot_manager.is_warm:
                    _pot_skip_reason = "not_warm"
                    pot_manager.ensure_warm_async()
                else:
                    pot_extractor_args = pot_manager.get_extractor_args()
                    if pot_extractor_args:
                        # pot_extractor_args 格式: "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416"
                        # 需要解析并注入到 extractor_args
                        extractor_args = cast(
                            dict[str, Any], ydl_opts.setdefault("extractor_args", {})
                        )

                        # 解析 "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416"
                        if ":" in pot_extractor_args:
                            ie_key, args_str = pot_extractor_args.split(":", 1)
                            pot_args: dict[str, Any] = extractor_args.setdefault(ie_key, {})

                            # 解析 "base_url=http://127.0.0.1:4416"
                            for part in args_str.split(";"):
                                if "=" in part:
                                    k, v = part.split("=", 1)
                                    pot_args[k] = [v]

                            pot_injected = True
                            _pot_skip_reason = ""
                            _base_url = (pot_args.get("base_url") or [""])[0]
                            self._emit_log(
                                "info",
                                f"[POT][Parse] 注入 base_url={_base_url} warm=True "
                                f"port={pot_manager.active_port} url={_pot_url_tag}",
                            )

                            # 首次激活时验证 yt-dlp 是否能加载 POT 插件
                            if not getattr(self, "_pot_plugin_checked", False):
                                self._pot_plugin_checked = True
                                try:
                                    plugin_ok, plugin_msg = pot_manager.verify_plugin_loadable()
                                    if plugin_ok:
                                        self._emit_log("info", f"✅ {plugin_msg}")
                                    else:
                                        self._emit_log(
                                            "warning",
                                            f"⚠️ POT 插件验证失败: {plugin_msg}。"
                                            "PO Token 服务已运行但可能无法被 yt-dlp 使用。",
                                        )
                                except Exception as diag_err:
                                    self._emit_log("debug", f"POT 插件诊断异常: {diag_err}")
            except Exception as e:
                _pot_skip_reason = "error"
                self._emit_log("debug", f"POT Provider 检测失败: {e}")

        if not pot_injected and _pot_skip_reason:
            self._emit_log(
                "info" if _pot_skip_reason in ("disabled", "twitter") else "warning",
                f"[POT][Parse] 跳过注入 reason={_pot_skip_reason} url={_pot_url_tag}",
            )
            # 这一轮不走 POT，就明确告诉 yt-dlp 别去取 Token。
            # 不加这个的话，bgutil 插件仍随 exe 被加载，拿不到 base_url 会自己
            # 回落到默认 127.0.0.1:4416 去 ping，然后刷一条 TransportError WARNING。
            # 那条 WARNING 会被 log_pot_from_output 转记成 [POT][YtDlp]，让"POT 关闭"
            # 的日志里出现 POT 相关噪音，正好污染这轮想要的自证能力。
            # 安全性：yt-dlp 自身没有内置 POT provider（-v 输出里只有 bgutil:cli /
            # bgutil:http 两个 external），所以 never 禁掉的恰好是本来也不可能成功的
            # 那次尝试，格式数量实测不变。twitter 不加——参数是 youtube 命名空间的。
            if not _is_twitter:
                _ea = cast(dict[str, Any], ydl_opts.setdefault("extractor_args", {}))
                _yt_args: dict[str, Any] = _ea.setdefault("youtube", {})
                _yt_args.setdefault("fetch_pot", ["never"])

        # --- 诊断日志：打印最终 extractor_args 概要 ---
        final_ea = ydl_opts.get("extractor_args", {})
        if final_ea:
            ea_summary = {
                k: list(v.keys()) if isinstance(v, dict) else v for k, v in final_ea.items()
            }
            self._emit_log("debug", f"[Final] extractor_args: {ea_summary}")

        # --- Optional: 手动 YouTube PO Token (备用方案) ---
        # 如果 POT Provider 未启用或未运行，用户可以手动配置静态 PO Token
        # Context: YouTube is rolling out PO Token enforcement. yt-dlp recommends using
        # the `mweb` client together with a PO Token when default clients fail.
        if not pot_injected:
            po_token = str(config_manager.get("youtube_po_token") or "").strip()
            if po_token:
                extractor_args = cast(dict[str, Any], ydl_opts.setdefault("extractor_args", {}))
                youtube_args = cast(dict[str, Any], extractor_args.setdefault("youtube", {}))

                # PO Token for mweb.gvs is typically session-bound; cookies are usually required.
                if not has_valid_cookie:
                    self._emit_log(
                        "warning",
                        "已配置 PO Token，但当前未加载有效 Cookies。mweb.gvs PO Token 通常需要配合 cookies 使用。",
                    )

                # Prefer adding mweb as a fallback client when token is present.
                # Use a single comma-separated value to match yt-dlp syntax.
                youtube_args["player_client"] = ["default,mweb"]
                youtube_args["po_token"] = [po_token]
                # Remove aggressive skips that are intended for no-cookie mobile simulation.
                # With PO Token, we want the most browser-like, complete extraction.
                youtube_args.pop("player_skip", None)
                self._emit_log("info", "🔐 已注入手动 PO Token：将优先尝试 mweb 客户端")

        # FFmpeg location
        ffmpeg_path = str(config_manager.get("ffmpeg_path") or "").strip()
        if ffmpeg_path:
            try:
                if Path(ffmpeg_path).exists():
                    ydl_opts["ffmpeg_location"] = ffmpeg_path
                else:
                    self._emit_log(
                        "warning", f"FFmpeg 自定义路径无效，已忽略并回退自动检测: {ffmpeg_path}"
                    )
            except Exception:
                self._emit_log(
                    "warning", f"FFmpeg 自定义路径无效，已忽略并回退自动检测: {ffmpeg_path}"
                )
        elif is_frozen():
            bundled_ffmpeg = find_bundled_executable(
                # New layout (preferred): dist/_internal/ffmpeg/ffmpeg.exe
                "ffmpeg.exe",
                # Legacy layout(s): assets/bin/ffmpeg/ffmpeg.exe
                "ffmpeg/ffmpeg.exe",
            )
            if bundled_ffmpeg is not None:
                # yt-dlp accepts either the ffmpeg.exe path or its containing folder.
                ydl_opts["ffmpeg_location"] = str(bundled_ffmpeg)
                self._emit_log("info", f"已启用内置 FFmpeg: {bundled_ffmpeg}")

        # === Phase 2: 核心下载层集成 ===

        # 并发分片数
        concurrent_fragments = config_manager.get("concurrent_fragments", 4)
        if concurrent_fragments and concurrent_fragments > 1:
            ydl_opts["concurrent_fragment_downloads"] = int(concurrent_fragments)

        # 下载限速
        rate_limit = str(config_manager.get("rate_limit") or "").strip()
        if rate_limit:
            ydl_opts["ratelimit"] = rate_limit

        # === 后处理：封面嵌入 & 元数据嵌入 ===
        download_thumbnail = config_manager.get("download_thumbnail", False)
        embed_thumbnail = config_manager.get("embed_thumbnail", False)
        embed_metadata = config_manager.get("embed_metadata", True)

        if download_thumbnail or embed_thumbnail or embed_metadata:
            postprocessors = ydl_opts.setdefault("postprocessors", [])

            # 封面嵌入或下载
            if download_thumbnail or embed_thumbnail:
                ydl_opts["writethumbnail"] = True
                # 转换缩略图格式为 jpg（兼容性最佳）
                ydl_opts["convert_thumbnail"] = "jpg"
                ydl_opts["__fluentytdl_keep_thumbnail"] = download_thumbnail
                ydl_opts["embedthumbnail"] = embed_thumbnail
                # 注意：不再添加 EmbedThumbnail 后处理器，由外部 thumbnail_embedder 处理

            # 元数据嵌入
            if embed_metadata:
                postprocessors.append({"key": "FFmpegMetadata"})

        # === SponsorBlock 广告跳过 ===
        sponsorblock_enabled = config_manager.get("sponsorblock_enabled", False)
        if sponsorblock_enabled:
            categories = config_manager.get(
                "sponsorblock_categories", ["sponsor", "selfpromo", "interaction"]
            )
            action = config_manager.get("sponsorblock_action", "remove")

            if categories:  # 确保有选中的类别
                if action == "mark":
                    ydl_opts["sponsorblock_mark"] = categories
                    self._emit_log(
                        "info",
                        f"🚫 SponsorBlock 已启用: 将标记以下类别为章节: {', '.join(categories)}",
                    )
                else:
                    # 默认为 remove
                    ydl_opts["sponsorblock_remove"] = categories
                    self._emit_log(
                        "info", f"🚫 SponsorBlock 已启用: 将移除以下类别: {', '.join(categories)}"
                    )

        # === SABR-only 兜底：追加 web_safari 客户端 ===
        # 必须放在 POT / 手动 PO Token 之后：那两条会设 player_client（如 default,mweb），
        # 这里是**追加**而非覆盖，让 default 客户端仍先跑、web_safari 作为拿回高清直链的
        # 兜底。仅在账号/会话被标记为 SABR-only 时生效，且仅 YouTube（player_client 是
        # youtube 命名空间参数）。这是唯一能同时覆盖解析与下载两条独立 opts 路径的位置。
        # 【待观察】当前只在检测到 SABR 时追加 web_safari，可能需按其他视频的解析情况调整。
        if not _is_twitter:
            self._maybe_append_sabr_web_safari(ydl_opts)

        return ydl_opts

    def _maybe_append_sabr_web_safari(self, ydl_opts: dict[str, Any]) -> None:
        """账号/会话被标记为 SABR-only 时，把 web_safari 追加进 player_client。

        追加语义（非锁定）：已有 player_client（如 POT 的 `default,mweb`）就在末尾补
        `web_safari` 并去重，无则设 `default,web_safari`。见 CLAUDE.md §4.1 的 SABR 例外。
        """
        try:
            from ..auth.auth_service import auth_service

            if not auth_service.get_youtube_sabr_only():
                return
        except Exception:
            return

        extractor_args = cast(dict[str, Any], ydl_opts.setdefault("extractor_args", {}))
        youtube_args = cast(dict[str, Any], extractor_args.setdefault("youtube", {}))

        # player_client 在本项目里一律存成"单元素、逗号分隔"的 list（见 POT 分支
        # `["default,mweb"]`），yt_dlp_cli / workers 拼命令行时按逗号 join。这里沿用同形。
        existing = youtube_args.get("player_client")
        if isinstance(existing, (list, tuple)) and existing:
            raw = ",".join(str(x) for x in existing)
        elif isinstance(existing, str):
            raw = existing
        else:
            raw = "default"

        clients = [c.strip() for c in raw.split(",") if c.strip()]
        if "web_safari" not in clients:
            clients.append("web_safari")
        youtube_args["player_client"] = [",".join(clients)]

        self._emit_log(
            "warning",
            "⚠️ [SABR] 该账号处于 SABR-only 灰度，已追加 web_safari 客户端以拿回高清直链 "
            f"(player_client={youtube_args['player_client'][0]})。此策略为待观察项。",
        )

    @staticmethod
    def _youtube_sabr_only_flag() -> bool:
        """读当前 YouTube 账号/会话的 SABR-only 标记（异常时保守返回 False）。"""
        try:
            from ..auth.auth_service import auth_service

            return auth_service.get_youtube_sabr_only()
        except Exception:
            return False

    def _maybe_reparse_after_sabr_flip(
        self,
        url: str,
        options: YoutubeServiceOptions | None,
        sabr_before: bool,
        cancel_event: threading.Event | None,
        *,
        tuned_overrides: dict[str, Any],
        extra_args: list[str],
    ) -> dict[str, Any] | None:
        """SABR 标记本趟刚翻转 → 用带 web_safari 的新 opts 重解析一次。

        仅当 `sabr_before is False` 且现在已翻转为 True 时触发一次；否则返回 None
        （调用方沿用原 info）。返回 None 也用于任何重解析失败——那时保留原始 360p
        结果总比整个弹窗解析失败好。
        """
        if sabr_before or not self._youtube_sabr_only_flag():
            return None
        try:
            self._emit_log(
                "info",
                "🔁 [SABR] 首次检测到账号级 SABR，正在追加 web_safari 客户端重新解析以拿回高清档…",
            )
            fresh = self.build_ydl_options(options, url=url)
            fresh.update(tuned_overrides)
            info = run_dump_single_json(
                url, fresh, extra_args=extra_args, cancel_event=cancel_event
            )
            if isinstance(info, dict):
                return info
        except YtDlpCancelled:
            raise
        except Exception as exc:
            self._emit_log("warning", f"[SABR] web_safari 重解析失败，沿用原结果: {exc}")
        return None

    def _maybe_configure_youtube_js_runtime(self, ydl_opts: dict[str, Any]) -> None:
        """Configure yt-dlp external JS runtime (YouTube EJS).

        Context: yt-dlp issue #15012 — YouTube support without an external JS runtime
        is deprecated and may miss formats (especially for logged-in users).
        """

        preferred = str(config_manager.get("js_runtime") or "auto").strip().lower()
        runtime_path = str(config_manager.get("js_runtime_path") or "").strip() or None
        runtime_path_ok = None
        if runtime_path:
            try:
                if Path(runtime_path).exists():
                    runtime_path_ok = runtime_path
            except Exception:
                runtime_path_ok = None

        # runtime_id → (自带目录里的候选相对路径, PATH 上的候选可执行名)
        # yt-dlp 用这些 runtime id；quickjs 的可执行文件通常叫 "qjs"。
        runtime_candidates: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
            "deno": (("deno.exe", "js/deno.exe", "deno/deno.exe"), ("deno",)),
            "node": (("node.exe", "js/node.exe", "node/node.exe"), ("node",)),
            "bun": (("bun.exe", "js/bun.exe", "bun/bun.exe"), ("bun",)),
            "quickjs": (("qjs.exe", "js/qjs.exe", "quickjs/qjs.exe"), ("qjs", "quickjs")),
        }

        def bundled_runtime_path(runtime_id: str) -> str | None:
            """自带目录里的 runtime，找不到返回 None。

            **不能再用 `is_frozen()` 设闸**：`assets/bin/deno/deno.exe` 在源码运行时
            同样存在，而 `prepare_yt_dlp_env()` 也照样把它注入子进程 PATH。加了闸门
            只会让 dev 和发行版走两条不同的代码路径 —— 开发时永远测不到发行版的分支，
            还会在 deno 明明就在自带目录里时警告"未检测到任何受支持的 JS runtime"。
            """
            entry = runtime_candidates.get(runtime_id)
            if entry is None:
                return None
            p = find_bundled_executable(*entry[0])
            return str(p) if p is not None else None

        def is_available(runtime_id: str) -> bool:
            """自定义路径 → 自带目录 → 系统 PATH，与 `locate_runtime_tool()` 同序。

            自带目录未命中时必须继续看 PATH：早先的版本在冻结分支里直接 return，
            结果是发行版完全无视用户装在 PATH 上的 runtime。
            """
            if runtime_path_ok and preferred == runtime_id:
                return True
            if bundled_runtime_path(runtime_id):
                return True
            entry = runtime_candidates.get(runtime_id)
            return bool(entry and any(shutil.which(n) for n in entry[1]))

        # If user specifies a runtime explicitly, honor it.
        if preferred in {"deno", "node", "bun", "quickjs"}:
            if is_available(preferred):
                cfg: dict[str, Any] = {}
                cfg["path"] = runtime_path_ok or bundled_runtime_path(preferred) or ""
                if not cfg["path"]:
                    cfg.pop("path", None)
                ydl_opts["js_runtimes"] = {preferred: cfg}
                self._emit_log("info", f"已启用 JS runtime: {preferred}")
            else:
                self._emit_log(
                    "warning",
                    f"未找到 JS runtime: {preferred}。请安装并加入 PATH（推荐 deno），或在设置中填写可执行文件路径。",
                )
            return

        # Auto mode:
        # - If we ship a bundled deno, use it (dev 与发行版同路径，见 bundled_runtime_path)。
        deno = bundled_runtime_path("deno")
        if deno:
            ydl_opts["js_runtimes"] = {"deno": {"path": deno}}
            self._emit_log("info", f"已启用内置 JS runtime: deno ({deno})")
            return

        # - If deno exists on PATH, do nothing (yt-dlp default enables deno).
        if any(shutil.which(n) for n in ["deno"]):
            return

        # - If deno is installed via winget but not on PATH, try to locate it.
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            try:
                winget_packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
                if winget_packages.exists():
                    matches = list(winget_packages.glob("DenoLand.Deno_*\\deno.exe"))
                    if matches:
                        deno_path = str(matches[0])
                        ydl_opts["js_runtimes"] = {"deno": {"path": deno_path}}
                        self._emit_log(
                            "warning",
                            f"检测到 winget 安装的 deno，但未在 PATH 中；已自动使用: {deno_path}",
                        )
                        return
            except Exception:
                pass

        # - Else, try other runtimes by order of recommendation.
        for runtime_id in ["node", "bun", "quickjs"]:
            if is_available(runtime_id):
                cfg: dict[str, Any] = {}
                # Auto 模式下不使用 js_runtime_path（避免用户填了 deno 路径却意外套到 node/bun 上）
                cfg["path"] = bundled_runtime_path(runtime_id) or ""
                if not cfg["path"]:
                    cfg.pop("path", None)
                ydl_opts["js_runtimes"] = {runtime_id: cfg}
                self._emit_log(
                    "warning",
                    f"未检测到 deno，已自动启用 {runtime_id} 作为 JS runtime（建议优先安装 deno）。",
                )
                return

        self._emit_log(
            "warning",
            "未检测到任何受支持的 JS runtime（deno/node/bun/quickjs）。YouTube 解析可能缺失大量格式。建议安装 deno 并加入 PATH。",
        )

    @staticmethod
    def _is_probably_json_cookie_file(path: str) -> bool:
        """Heuristic: exported cookies must be Netscape format (plain text), not JSON."""

        try:
            with open(path, "rb") as f:
                head = f.read(2048)
            text = head.decode("utf-8", errors="ignore").lstrip()
            if not text:
                return False
            # Common JSON exports start with '{' or '['
            if text[0] in "[{":
                return True
            # Netscape cookie file typically starts with a comment header.
            if text.startswith("# Netscape HTTP Cookie File"):
                return False
            return False
        except Exception:
            return False

    @staticmethod
    def _count_platform_cookies(path: str, platform: str) -> int:
        """Count cookies for a specific platform."""
        PLATFORM_DOMAINS = {
            "youtube": ("youtube", "google"),
            "twitter": ("twitter.com", "x.com"),
        }
        domains = PLATFORM_DOMAINS.get(platform, ())
        try:
            jar = http.cookiejar.MozillaCookieJar()
            jar.load(path, ignore_discard=True, ignore_expires=True)
            count = 0
            for c in jar:
                domain = (c.domain or "").lower()
                if any(d in domain for d in domains):
                    count += 1
            return count
        except Exception:
            return 0

    @staticmethod
    def _count_youtube_related_cookies(path: str) -> int:
        """Count YouTube/Google related cookies in a Netscape cookie file."""

        try:
            jar = http.cookiejar.MozillaCookieJar()
            jar.load(path, ignore_discard=True, ignore_expires=True)
            count = 0
            for c in jar:
                domain = (c.domain or "").lower()
                if "youtube" in domain or "google" in domain:
                    count += 1
            return count
        except Exception:
            return 0

    # ========== VR 视频智能检测与二次解析 ==========

    # VR 相关关键词 (用于标题/描述检测)
    _VR_KEYWORDS = (
        "vr180",
        "vr360",
        "vr 180",
        "vr 360",
        "180°",
        "360°",
        "180vr",
        "360vr",
        "3d vr",
        "vr video",
        "vr体验",
        "vr视频",
        "sbs",
        "side by side",
        "over under",
        "ou3d",
        "stereoscopic",
        "immersive",
    )

    def _is_vr_video(self, info: dict[str, Any]) -> bool:
        """检测视频是否为 VR180/VR360 内容。

        检测条件:
        1. 标题/描述含 VR 相关关键词
        2. 格式列表中包含 'mesh' 标记 (VR 投影格式特征)
        3. 标题声称 8K 但最高格式 < 4320p (格式异常)
        """
        title = str(info.get("title") or "").lower()
        description = str(info.get("description") or "").lower()
        text = f"{title} {description}"

        # 检查 VR 关键词
        for kw in self._VR_KEYWORDS:
            if kw in text:
                self._emit_log("info", f"🥽 检测到 VR 关键词: '{kw}'")
                return True

        # 检查格式是否包含 mesh 标记 (VR 投影)
        formats = info.get("formats") or []
        for fmt in formats:
            format_note = str(fmt.get("format_note") or "").lower()
            format_id = str(fmt.get("format") or "").lower()
            if "mesh" in format_note or "mesh" in format_id:
                self._emit_log("info", "🥽 检测到 VR 投影格式 (mesh)")
                return True

        # 检查分辨率异常: 标题含 8K 但格式列表最高 < 4320p
        if "8k" in title:
            max_height = 0
            for fmt in formats:
                h = fmt.get("height") or 0
                if isinstance(h, int) and h > max_height:
                    max_height = h
            if max_height > 0 and max_height < 4320:
                self._emit_log(
                    "warning",
                    f"⚠️ 标题声称 8K 但最高格式仅 {max_height}p，可能是 VR 视频",
                )
                return True

        return False

    def _get_max_resolution(self, info: dict[str, Any]) -> int:
        """获取格式列表中的最高分辨率 (height)"""
        formats = info.get("formats") or []
        max_height = 0
        for fmt in formats:
            h = fmt.get("height") or 0
            if isinstance(h, int) and h > max_height:
                max_height = h
        return max_height

    def _detect_vr_projection(self, info: dict[str, Any]) -> None:
        """分析 VR 视频格式的投影类型和立体模式，逐格式标注。

        为每个视频格式注入:
          __vr_projection:  "equirectangular" | "mesh" | "eac" | "unknown"
          __vr_stereo_mode: "mono" | "stereo_tb" | "stereo_sbs" | "unknown"

        同时在 info["__vr_projection_summary"] 写入整体概览。
        """
        title = str(info.get("title") or "").lower()
        description = str(info.get("description") or "").lower()
        text = f"{title} {description}"

        # 标题/描述辅助信号
        title_hints_sbs = any(kw in text for kw in ("sbs", "side by side", "side-by-side"))
        title_hints_vr180 = "vr180" in text or "vr 180" in text or "180°" in text
        title_hints_360 = any(
            kw in text for kw in ("360°", "vr360", "vr 360", "360vr", "360 video")
        )
        title_hints_stereo = any(
            kw in text
            for kw in (
                "3d",
                "stereo",
                "stereoscopic",
                "over under",
                "over-under",
                "ou3d",
                "top bottom",
                "top-bottom",
            )
        )

        formats = info.get("formats") or []

        # 统计
        projections: dict[str, int] = {}
        stereo_modes: dict[str, int] = {}
        has_equi = False
        has_eac = False
        has_mesh = False
        max_height_fmt: dict[str, Any] | None = None
        max_height = 0

        for fmt in formats:
            # 跳过纯音频
            vcodec = str(fmt.get("vcodec") or "none").lower()
            if vcodec == "none":
                continue

            width = fmt.get("width") or 0
            height = fmt.get("height") or 0
            format_note = str(fmt.get("format_note") or "").lower()
            format_field = str(fmt.get("format") or "").lower()

            # ---- 投影类型检测 ----
            projection = "unknown"
            if "mesh" in format_note or "mesh" in format_field:
                projection = "mesh"
            elif width > 0 and height > 0:
                ratio = width / height
                # avc1 (H.264) 几乎都是标准 Equirectangular
                is_legacy_codec = vcodec.startswith("avc1")
                if is_legacy_codec:
                    projection = "equirectangular"
                elif 1.9 <= ratio <= 2.1:
                    # 2:1 → 标准 Equirectangular
                    projection = "equirectangular"
                elif 0.9 <= ratio <= 1.1:
                    # 1:1 → 通常是 Equirectangular 的 TB 立体
                    projection = "equirectangular"
                else:
                    # 非标准比例 + 高端编码 → 可能是 EAC
                    # EAC 常见比例: 约 1.5:1 (3840×2560) 或 3:2
                    if 1.3 <= ratio <= 1.7 and not is_legacy_codec:
                        projection = "eac"
                    else:
                        projection = "unknown"

            # ---- 立体模式检测 ----
            stereo = "unknown"
            if projection == "mesh":
                # Mesh 投影基本都是 VR180 SBS (鱼眼)
                stereo = "stereo_sbs"
            elif width > 0 and height > 0:
                ratio = width / height
                if 0.9 <= ratio <= 1.1:
                    # 1:1 宽高比 → Top-Bottom 立体 (上下各一半是 2:1 画面)
                    stereo = "stereo_tb"
                elif 1.9 <= ratio <= 2.1:
                    # 2:1 → 默认是 Mono 360°
                    # 但标题暗示立体的话，可能是 SBS
                    if title_hints_sbs or (title_hints_stereo and not title_hints_360):
                        stereo = "stereo_sbs"
                    else:
                        stereo = "mono"
                elif 3.4 <= ratio <= 3.6:
                    # 旧标准 SBS (极少见)
                    stereo = "stereo_sbs"
                elif projection == "eac":
                    # EAC 的立体判断需依赖标题
                    if title_hints_stereo or title_hints_vr180:
                        stereo = "stereo_tb"
                    else:
                        stereo = "mono"

            fmt["__vr_projection"] = projection
            fmt["__vr_stereo_mode"] = stereo

            # 统计
            projections[projection] = projections.get(projection, 0) + 1
            stereo_modes[stereo] = stereo_modes.get(stereo, 0) + 1

            if projection == "equirectangular":
                has_equi = True
            if projection == "eac":
                has_eac = True
            if projection == "mesh":
                has_mesh = True

            h = int(height) if isinstance(height, (int, float)) else 0
            if h > max_height:
                max_height = h
                max_height_fmt = fmt

        # 整体概览
        primary_proj = "unknown"
        primary_stereo = "unknown"
        if max_height_fmt is not None:
            primary_proj = str(max_height_fmt.get("__vr_projection") or "unknown")
            primary_stereo = str(max_height_fmt.get("__vr_stereo_mode") or "unknown")

        has_stereo_3d = any(k.startswith("stereo") for k in stereo_modes if stereo_modes[k] > 0)
        has_mono = stereo_modes.get("mono", 0) > 0

        summary = {
            "primary_stereo": primary_stereo,
            "primary_projection": primary_proj,
            "has_stereo_3d": has_stereo_3d,
            "has_mono_360": has_mono,
            "has_eac": has_eac,
            "has_mesh": has_mesh,
            "has_equi_stream": has_equi,
            "eac_only": has_eac and not has_equi and not has_mesh,
            "max_height": max_height,
        }
        info["__vr_projection_summary"] = summary

        # 日志
        stereo_label = {
            "mono": "2D 全景",
            "stereo_tb": "3D 立体 (上下)",
            "stereo_sbs": "3D 立体 (左右/Mesh)",
        }.get(primary_stereo, "未知")
        proj_label = {
            "equirectangular": "Equirectangular",
            "mesh": "Mesh (鱼眼)",
            "eac": "EAC (立方体)",
        }.get(primary_proj, "未知")
        self._emit_log(
            "info",
            f"🥽 [VR] 投影检测: {stereo_label} / {proj_label}"
            f" (Equi={has_equi}, Mesh={has_mesh}, EAC={has_eac})",
        )

    def _extract_vr_formats(
        self,
        url: str,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        """使用 android_vr 客户端获取 VR 高分辨率格式。

        注意: android_vr 不支持 cookies，因此无法用于年龄验证。
        此方法仅用于补充 VR 高分辨率格式。
        """
        self._emit_log("info", "🔄 使用 android_vr 客户端获取 VR 高分辨率格式...")

        # 构建无 cookies 的 android_vr 解析选项
        vr_opts: dict[str, Any] = {
            # 同上：`quiet` / `no_warnings` 没有 CLI 映射，是死配置，已删。
            "ignoreerrors": False,
            "skip_download": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android_vr"],
                }
            },
        }

        # FFmpeg location (复用主配置)
        ffmpeg_path = str(config_manager.get("ffmpeg_path") or "").strip()
        if ffmpeg_path and Path(ffmpeg_path).exists():
            vr_opts["ffmpeg_location"] = ffmpeg_path
        elif is_frozen():
            bundled_ffmpeg = find_bundled_executable("ffmpeg.exe", "ffmpeg/ffmpeg.exe")
            if bundled_ffmpeg is not None:
                vr_opts["ffmpeg_location"] = str(bundled_ffmpeg)

        # JS runtime (复用主配置逻辑)
        self._maybe_configure_youtube_js_runtime(vr_opts)

        try:
            info = run_dump_single_json(
                url,
                vr_opts,
                extra_args=["--no-playlist"],
                cancel_event=cancel_event,
            )
            if isinstance(info, dict):
                formats = info.get("formats") or []
                self._emit_log(
                    "info",
                    f"✅ android_vr 客户端获取到 {len(formats)} 个格式",
                )
                return list(formats)
        except Exception as e:
            self._emit_log("warning", f"android_vr 解析失败: {e}")

        return []

    def _merge_formats(
        self,
        info: dict[str, Any],
        vr_formats: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """合并首次解析和 VR 解析的格式列表。

        合并策略:
        - 使用 format_id 去重
        - VR 格式优先 (通常包含更高分辨率)
        - 记录 VR 专属格式 ID (仅 android_vr 有，web 没有)
        - 记录所有 android_vr 可用格式 ID (用于兼容性检查)
        """
        if not vr_formats:
            return info

        existing_formats = info.get("formats") or []
        existing_ids = {str(f.get("format_id") or "") for f in existing_formats}

        # 记录所有 android_vr 可用的格式 ID (用于下载时兼容性检查)
        all_vr_format_ids: list[str] = []
        for vr_fmt in vr_formats:
            fmt_id = str(vr_fmt.get("format_id") or "")
            if fmt_id:
                all_vr_format_ids.append(fmt_id)

        # 添加不重复的 VR 格式，并记录 VR 专属格式 ID
        added_count = 0
        vr_only_format_ids: list[str] = []
        for vr_fmt in vr_formats:
            fmt_id = str(vr_fmt.get("format_id") or "")
            if fmt_id and fmt_id not in existing_ids:
                existing_formats.append(vr_fmt)
                existing_ids.add(fmt_id)
                vr_only_format_ids.append(fmt_id)
                added_count += 1

        if added_count > 0:
            # 按分辨率排序
            existing_formats.sort(key=lambda f: (f.get("height") or 0, f.get("width") or 0))
            info["formats"] = existing_formats

            # 记录 VR 专属格式 ID (仅 android_vr 有，web 没有)
            info["__vr_only_format_ids"] = vr_only_format_ids
            # 记录所有 android_vr 可用格式 ID (用于下载时兼容性检查)
            info["__android_vr_format_ids"] = all_vr_format_ids
            self._emit_log(
                "info",
                f"✅ 已合并 {added_count} 个 VR 高分辨率格式 (IDs: {', '.join(vr_only_format_ids)})",
            )

            # 更新最高分辨率信息
            max_height = self._get_max_resolution(info)
            if max_height >= 4320:
                self._emit_log("info", f"🎉 最高可用分辨率: {max_height}p (8K)")
            elif max_height >= 2160:
                self._emit_log("info", f"📺 最高可用分辨率: {max_height}p (4K)")

        return info

    def extract_info_sync(
        self,
        url: str,
        options: YoutubeServiceOptions | None = None,
        *,
        read_cache: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Blocking metadata extraction (call from worker thread).

        这是播放列表逐条深解析（EntryDetailWorker）的落点，会被同一批 URL 反复调用
        （配置窗口爬一遍、选择弹窗再爬一遍），因此接入 P2.1 的 entry_detail 缓存。

        `read_cache=False`：封面模式专用。封面会把 `thumbnails[].url` 直接当成下载
        任务的 URL 发出去，命中缓存等于发一条最旧可达 TTL 的陈旧直链。**跳过读、
        照常写** —— 这一趟本来就是新鲜的，写回去顺带刷新条目，对其他模式只有好处。
        """

        ydl_opts = self.build_ydl_options(options, url=url)

        # === TTL 缓存查询（P2.1：播放列表逐条深解析）===
        _t_total = time.perf_counter()
        cache_key = self._parse_cache_key(url, "entry_detail", ydl_opts)
        cached = self._parse_cache_get(cache_key) if read_cache else None
        if cached is not None:
            cached_info, age = cached
            self._log_parse_cache_hit(
                "entry_detail",
                url,
                cached_info,
                age,
                (time.perf_counter() - _t_total) * 1000,
            )
            return cached_info

        try:
            _ = locate_runtime_tool("yt-dlp.exe", "yt-dlp/yt-dlp.exe", "yt_dlp/yt-dlp.exe")
        except FileNotFoundError as e:
            raise FileNotFoundError(
                "未找到 yt-dlp.exe。请在设置页指定路径，或将 yt-dlp.exe 放入 _internal/yt-dlp/，或加入 PATH。"
            ) from e

        def _do_extract(opts: dict[str, Any]) -> dict[str, Any]:
            self._emit_log("info", f"[EXE] 开始解析 URL: {url}")
            info = run_dump_single_json(
                url, opts, extra_args=["--no-playlist"], cancel_event=cancel_event
            )
            if info is None or info is False:
                raise RuntimeError(
                    "解析失败：yt-dlp 未返回有效元数据（可能被要求登录/验证）。"
                    "请在弹窗中启用浏览器 Cookies 重试。"
                )
            if not isinstance(info, dict):
                raise RuntimeError(f"yt-dlp returned unexpected info type: {type(info)!r}")
            return cast(dict[str, Any], info)

        _sabr_before = self._youtube_sabr_only_flag()

        try:
            self._emit_log("info", f"开始解析 URL: {url}")
            info = _do_extract(ydl_opts)
            # SABR 标记本趟刚翻转：重建 opts（含 web_safari）重解析一次拿回高清档。
            retried = self._maybe_reparse_after_sabr_flip(
                url,
                options,
                _sabr_before,
                cancel_event,
                tuned_overrides={},
                extra_args=["--no-playlist"],
            )
            if retried is not None:
                info = retried
            self._parse_cache_put(cache_key, info)
            return info
        except Exception as exc:
            if isinstance(exc, YtDlpCancelled):
                raise
            msg = str(exc)
            lower = msg.lower()

            # 注意: 已移除 cookies-from-browser fallback
            # 所有 Cookie 统一通过 Cookie Sentinel 管理，使用 bin/cookies.txt
            # 这样可以避免 DPAPI 文件锁和权限问题

            if "not a bot" in lower or "sign in" in lower:
                proxy_mode = str(config_manager.get("proxy_mode") or "off").lower().strip()
                proxy_url = str(config_manager.get("proxy_url") or "").strip()
                if proxy_mode in {"http", "socks5"} and proxy_url:
                    msg = (
                        msg
                        + "\n\n提示: 检测到已启用代理，部分代理/出口 IP 会显著增加平台风控概率。"
                        + "建议在设置中临时关闭代理后重试解析。"
                    )

                from ..utils.url_router import UrlRouter

                if UrlRouter.detect_platform(url) == "twitter":
                    msg += "\n\n提示: X 平台需要登录才能下载部分内容。请在设置中登录 X 平台获取 Cookie。"
                else:
                    msg = (
                        msg
                        + "\n\n提示: YouTube 会在浏览器标签页中频繁轮换账号 cookies。官方建议用无痕/隐私窗口登录后导出 youtube.com cookies，并立即关闭无痕窗口，以避免 cookies 被轮换。"
                        + "\n提示: YouTube 正在逐步强制 PO Token。若仅靠 cookies 仍触发验证，可在设置中填写 PO Token，并让 yt-dlp 走 mweb 客户端（官方推荐路径）。"
                    )

            self._emit_log("error", f"解析失败: {msg}")
            raise RuntimeError(msg) from exc

    # ==================== 解析结果 TTL 缓存（P2 / P2.1 / P2.2） ====================
    # 覆盖范围（五个 mode，各自独立限容）：
    #   dialog        extract_info_for_dialog_sync  弹窗解析（视频 / 字幕 / 封面共用）
    #   playlist_flat extract_playlist_flat         播放列表 flat 列举
    #   entry_detail  extract_info_sync             播放列表逐条深解析
    #   vr            extract_vr_info_sync          android_vr 深解析
    #   channel_tab   extract_channel_flat          频道单标签页 flat 列举
    #
    # 下载路径绝不读缓存——必须拿新鲜签名 URL，否则 403。这条边界是结构性的：
    # download/executor.py 只 import youtube.yt_dlp_cli，自己拼 argv，从不调用本类。
    # entry_detail / vr / channel_tab 三个 mode 的调用方也只有 EntryDetailWorker /
    # VRInfoExtractWorker / ChannelExtractWorker 这几条纯 UI 链路。
    # 新增 mode 前请先确认调用方不在下载取流路径上。
    #
    # 例外：**封面模式全程不读缓存**（read_cache=False）。封面会把 info 里的
    # thumbnails[].url 直接当成下载任务的 URL 发出去（见 download_config_window 的
    # cover 分支 → DownloadWorker._run_cover_direct_download），命中缓存等于发一条
    # 最旧可达 parse_cache_ttl_seconds 的陈旧直链。写入照常，读取跳过。

    @staticmethod
    def _parse_cache_enabled() -> bool:
        return bool(config_manager.get("parse_cache_enabled", True))

    @staticmethod
    def _parse_cache_ttl() -> float:
        """解析结果的保留时长（秒）。0 表示不保留，读写一并停掉。

        默认值必须与 config_manager.DEFAULT_CONFIG 保持一致（0 = 不保留），
        设置页「解析结果保留时间」写的就是这个键。缺键和脏值都退到 0：这一层
        的失效条件还不完备（换出口 IP 撞不掉键），拿不准时宁可多跑一次子进程。
        """
        try:
            ttl = float(config_manager.get("parse_cache_ttl_seconds", 0))
        except (TypeError, ValueError):
            ttl = 0.0
        return max(0.0, ttl)

    @staticmethod
    def _cookie_fingerprint(path: str | None) -> str:
        """Cookie 指纹：只用路径 + 元数据 sidecar 的 mtime/大小，绝不读取内容。

        关键：不能用 cookie jar 自身的 mtime/大小。yt-dlp 每次 `--cookies` 运行结束
        都会把 jar 回写一遍（数量还会随会话变化），jar 的 mtime 每次解析都在变，
        拿它做指纹等于让缓存永远 miss。

        `cookies_<platform>.txt.meta` 只有 CookieSentinel 真正刷新凭据时才写
        （`_save_meta`），正好就是"Cookie 语义变了"的信号；yt-dlp 的回写不碰它。
        sidecar 不存在时（手动导入的 cookie 文件）退回 jar 自身的 mtime——
        此时没有回写放大问题，因为这类文件不是 sentinel 托管的。
        """
        if not path:
            return "-"
        meta = f"{path}.meta"
        try:
            st = os.stat(meta)
            return f"{path}:meta:{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            pass
        try:
            st = os.stat(path)
            return f"{path}:jar:{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            return f"{path}:missing"

    def _parse_cache_key(self, url: str, mode: str, ydl_opts: dict[str, Any]) -> str:
        """按"影响解析结果"的少数字段构造键，其余（重试次数、超时等）不参与。

        键带 `mode:` 明文前缀：分桶淘汰要靠它区分归属，日志也能按 mode grep。
        """
        payload = {
            "url": url.strip(),
            "mode": mode,
            "cookie": self._cookie_fingerprint(ydl_opts.get("cookiefile")),
            "proxy": ydl_opts.get("proxy") or "",
            # extractor_args 里含 player_client / POT base_url / skip=authcheck，
            # 它们都会改变返回的格式列表，必须进指纹。
            "extractor_args": ydl_opts.get("extractor_args") or {},
            "format_sort": ydl_opts.get("format_sort") or [],
            "format": ydl_opts.get("format") or "",
            "extract_flat": ydl_opts.get("extract_flat"),
            # 音轨意图从 opts 读而不是从 config 读：`format_sort` 里已经没有 `lang:`
            # 条目了，这两个键是它的替代品，不进指纹就等于换了策略还命中旧缓存。
            "audio_langs": ydl_opts.get("_fytdl_audio_langs") or [],
            "audio_strategy": ydl_opts.get("_fytdl_audio_strategy") or "",
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return f"{mode}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"

    @classmethod
    def _parse_cache_limit_for(cls, mode: str) -> int:
        if mode == "entry_detail":
            try:
                configured = int(config_manager.get("parse_cache_entry_max", 64))
            except (TypeError, ValueError):
                configured = 64
            if configured > 0:
                return configured
        return cls._PARSE_CACHE_LIMITS.get(mode, cls._PARSE_CACHE_MAX)

    def _parse_cache_buckets(self) -> dict[str, int]:
        """各 mode 桶的当前条目数，用于验收「大列表爬取没有挤掉弹窗结果」。"""
        counts: dict[str, int] = {}
        with self._parse_cache_lock:
            keys = list(self._parse_cache.keys())
        for k in keys:
            mode = k.split(":", 1)[0]
            counts[mode] = counts.get(mode, 0) + 1
        return counts

    def _log_parse_cache_hit(
        self, mode: str, url: str, info: dict[str, Any], age: float, total_ms: float
    ) -> None:
        buckets = self._parse_cache_buckets()
        detail = " ".join(f"{m}={n}" for m, n in sorted(buckets.items()))
        self._emit_log(
            "info",
            f"[ParseCache] 命中 mode={mode}: {_short_url_tag(url)} 缓存年龄 {age:.1f}s "
            f"本次耗时 {total_ms:.0f}ms (formats={len(info.get('formats') or [])}, "
            f"entries={len(info.get('entries') or [])}) 桶: {detail}",
        )

    def _parse_cache_get(self, key: str) -> tuple[dict[str, Any], float] | None:
        if not self._parse_cache_enabled():
            return None
        ttl = self._parse_cache_ttl()
        if ttl <= 0:
            return None
        now = time.monotonic()
        with self._parse_cache_lock:
            hit = self._parse_cache.get(key)
            if hit is None:
                return None
            stored_at, info = hit
            age = now - stored_at
            if age > ttl:
                self._parse_cache.pop(key, None)
                return None
            self._parse_cache.move_to_end(key)
        # 返回副本，避免调用方（如 _detect_vr_projection / _merge_formats）就地改写污染缓存
        return copy.deepcopy(info), age

    @staticmethod
    def _pot_state_unstable() -> bool:
        """POT 开着但还没预热好 —— 这一轮的解析结果不该进缓存。

        两个理由，缺一不可：
        1. **键会漂**：未预热时 build_ydl_options 注入的是 `youtube:fetch_pot=never`，
           预热完成后换成 `youtubepot-bgutilhttp:base_url=...`。extractor_args 在指纹里，
           所以预热期写进去的条目在预热完成后永远命不中，纯占桶。
           实测：启动后第一次爬播放列表正好落在这个窗口里，而这恰是 P2.1 要救的场景。
        2. **内容可能是降级的**：无 POT 解析可能少格式/被限速，预热完成后再把它端出来
           属于以次充好。宁可这几秒不缓存。

        POT 关闭时 `fetch_pot=never` 恒定存在，键本来就稳，不受此限制。
        """
        if not config_manager.get("pot_provider_enabled", False):
            return False
        try:
            from .pot_manager import pot_manager

            return not pot_manager.is_warm
        except Exception:
            return False

    def _parse_cache_put(self, key: str, info: dict[str, Any]) -> None:
        if not self._parse_cache_enabled() or self._parse_cache_ttl() <= 0:
            return
        if not isinstance(info, dict) or not info:
            return
        if self._pot_state_unstable():
            self._emit_log("debug", "[ParseCache] 跳过写入: POT 预热中，键不稳定")
            return
        n_entries = len(info.get("entries") or [])
        if n_entries > self._PARSE_CACHE_MAX_ENTRIES:
            self._emit_log(
                "debug",
                f"[ParseCache] 跳过写入: entries={n_entries} 超过上限 "
                f"{self._PARSE_CACHE_MAX_ENTRIES}，deepcopy 成本高于收益",
            )
            return
        mode = key.split(":", 1)[0]
        limit = self._parse_cache_limit_for(mode)
        snapshot = copy.deepcopy(info)
        with self._parse_cache_lock:
            self._parse_cache[key] = (time.monotonic(), snapshot)
            self._parse_cache.move_to_end(key)
            # 只在同 mode 桶内淘汰：OrderedDict 本身就是全局 LRU 顺序，
            # 顺序遍历取第一个同前缀的键即该桶最旧的一条。
            prefix = f"{mode}:"
            while True:
                same_mode = [k for k in self._parse_cache if k.startswith(prefix)]
                if len(same_mode) <= limit:
                    break
                self._parse_cache.pop(same_mode[0], None)

    def invalidate_parse_cache(self, reason: str = "") -> int:
        """清空弹窗解析缓存（用户手动重试 / Cookie 重新注入等场景调用）。

        返回清掉的条目数，供设置页的「立即清空」区分"清了 N 条"和"本来就是空的"。

        粒度只有"全清"：键是 sha256 指纹，URL 不可反查，按 URL 精细失效得先另建
        一份正向索引。这也是"画质不达标自动失效"暂时没接的前置条件——全局清会把
        无关链接的条目一起冲掉。
        """
        with self._parse_cache_lock:
            n = len(self._parse_cache)
            self._parse_cache.clear()
        if n:
            self._emit_log(
                "info", f"[ParseCache] 已清空 {n} 条缓存" + (f" ({reason})" if reason else "")
            )
        return n

    def extract_info_for_dialog_sync(
        self,
        url: str,
        options: YoutubeServiceOptions | None = None,
        *,
        read_cache: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Metadata extraction tuned for UI dialogs.

        Goals:
        - single video: keep full formats list (for manual format selection)
        - playlist: enumerate entries fast without per-entry deep extraction

        `read_cache=False`：封面模式专用，理由同 `extract_info_sync`——选中的
        `thumbnails[].url` 会直接变成下载任务的 URL。**跳过读、照常写。**
        """

        _t_total = time.perf_counter()

        _t_opts = time.perf_counter()
        ydl_opts = self.build_ydl_options(options, url=url)
        _opts_ms = (time.perf_counter() - _t_opts) * 1000
        tuned = dict(ydl_opts)

        tuned.update(
            {
                "skip_download": True,
                # yt-dlp supports string modes; "in_playlist" keeps single-video extraction intact
                "extract_flat": "in_playlist",
                "lazy_playlist": True,
                "ignoreerrors": False,
            }
        )

        if bool(config_manager.get("playlist_skip_authcheck") or False):
            tuned = self._with_youtubetab_skip_authcheck(tuned)

        self._emit_log("info", f"[DialogExtract] 开始解析: {url}")

        # === TTL 缓存查询（仅弹窗路径）===
        cache_key = self._parse_cache_key(url, "dialog", tuned)
        cached = self._parse_cache_get(cache_key) if read_cache else None
        if cached is not None:
            cached_info, age = cached
            self._log_parse_cache_hit(
                "dialog", url, cached_info, age, (time.perf_counter() - _t_total) * 1000
            )
            return cached_info

        def _log_done(info: dict[str, Any] | None, *, attempts: int, note: str = "") -> None:
            """统一的解析完成埋点（P0 基线）+ 结果入缓存（P2）。"""
            total_ms = (time.perf_counter() - _t_total) * 1000
            n_fmt = len(info.get("formats") or []) if isinstance(info, dict) else 0
            n_entries = len(info.get("entries") or []) if isinstance(info, dict) else 0
            if isinstance(info, dict):
                self._parse_cache_put(cache_key, info)
            self._emit_log(
                "info",
                f"[DialogExtract] 解析完成: {url} 耗时 {total_ms / 1000:.2f}s "
                f"(build_opts={_opts_ms:.0f}ms, 子进程轮次={attempts}, "
                f"formats={n_fmt}, entries={n_entries})" + (f" {note}" if note else ""),
            )

        def _log_fail(exc: BaseException, *, attempts: int) -> None:
            total_ms = (time.perf_counter() - _t_total) * 1000
            self._emit_log(
                "warning",
                f"[DialogExtract] 解析失败: {url} 耗时 {total_ms / 1000:.2f}s "
                f"(build_opts={_opts_ms:.0f}ms, 子进程轮次={attempts}) {type(exc).__name__}",
            )

        try:
            _ = locate_runtime_tool("yt-dlp.exe", "yt-dlp/yt-dlp.exe", "yt_dlp/yt-dlp.exe")
        except FileNotFoundError as e:
            raise FileNotFoundError(
                "未找到 yt-dlp.exe。请在设置页指定路径，或将 yt-dlp.exe 放入 _internal/yt-dlp/，或加入 PATH。"
            ) from e

        # 记下解析前的 SABR 标记：这一趟可能是 web_creator 高清被 SABR 丢光、只回 360p，
        # 而 run_dump_single_json 会在输出里读到 SABR 标记并给账号打标（首次翻转）。
        _sabr_before = self._youtube_sabr_only_flag()

        try:
            info = run_dump_single_json(
                url,
                tuned,
                extra_args=["--flat-playlist", "--lazy-playlist"],
                cancel_event=cancel_event,
            )
            info = cast(dict[str, Any], info)

            # SABR 标记本趟刚翻转（False→True）：此刻手里的 info 很可能只有 360p。
            # 立即用**重新构建**的 opts（build_ydl_options 会追加 web_safari）重解析一次，
            # 让用户当场看到高清档。缓存 key 含 extractor_args，天然不撞旧的 360p 结果。
            retried = self._maybe_reparse_after_sabr_flip(
                url,
                options,
                _sabr_before,
                cancel_event,
                tuned_overrides={
                    "skip_download": True,
                    "extract_flat": "in_playlist",
                    "lazy_playlist": True,
                    "ignoreerrors": False,
                },
                extra_args=["--flat-playlist", "--lazy-playlist"],
            )
            if retried is not None:
                info = retried
                _log_done(info, attempts=2, note="[SABR 追加 web_safari 重解析]")
                return info

            _log_done(info, attempts=1)
            return info
        except Exception as exc:
            if isinstance(exc, YtDlpCancelled):
                raise
            msg = str(exc)

            if self._is_page_reload_error(msg) and self._try_refresh_cookie_for_reload_error(url):
                info = run_dump_single_json(
                    url,
                    tuned,
                    extra_args=["--flat-playlist", "--lazy-playlist"],
                    cancel_event=cancel_event,
                )
                _log_done(cast(dict[str, Any], info), attempts=2, note="[cookie 刷新后重试]")
                return cast(dict[str, Any], info)

            if self._should_retry_with_youtubetab_skip_authcheck(msg):
                retry_opts = self._with_youtubetab_skip_authcheck(tuned)
                if retry_opts is not tuned:
                    self._emit_log(
                        "warning",
                        "检测到播放列表 authcheck 限制提示，按 yt-dlp 官方建议自动启用 youtubetab:skip=authcheck 并重试一次。",
                    )
                    info = run_dump_single_json(
                        url,
                        retry_opts,
                        extra_args=["--flat-playlist", "--lazy-playlist"],
                        cancel_event=cancel_event,
                    )
                    _log_done(cast(dict[str, Any], info), attempts=2, note="[skip=authcheck 重试]")
                    return cast(dict[str, Any], info)

            if self._is_auth_blocked_error(msg):
                self._emit_log(
                    "warning", "🔄 检测到认证封锁，丢弃 Cookie 使用无登录态客户端重试..."
                )
                fallback_opts = dict(tuned)
                fallback_opts.pop("cookiefile", None)
                fb_ea = fallback_opts.setdefault("extractor_args", {})
                fb_yt = fb_ea.setdefault("youtube", {})
                fb_yt["player_client"] = ["ios,mweb"]
                fb_yt.pop("player_skip", None)

                try:
                    info = run_dump_single_json(
                        url,
                        fallback_opts,
                        extra_args=["--flat-playlist", "--lazy-playlist"],
                        cancel_event=cancel_event,
                    )
                    if info:
                        self._emit_log("info", "✅ 无登录态降级解析成功（格式列表可能不完整）")
                        _log_done(cast(dict[str, Any], info), attempts=2, note="[无登录态降级]")
                        return cast(dict[str, Any], info)
                except Exception as fallback_exc:
                    self._emit_log("warning", f"降级解析也失败: {fallback_exc}")

            fallback_info = self._handle_channel_tab_fallback(
                url, msg, tuned, ["--flat-playlist", "--lazy-playlist"], cancel_event
            )
            if fallback_info:
                _log_done(fallback_info, attempts=2, note="[频道标签降级]")
                return fallback_info

            _log_fail(exc, attempts=1)
            raise

    def extract_vr_info_sync(
        self,
        url: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """VR 专用解析：使用纯 android_vr 客户端提取完整 VR 格式。

        与普通解析不同：
        - 固定使用 android_vr 客户端
        - 不使用 Cookies（android_vr 不支持）
        - 返回的格式包含完整的 SBS/OU/Mesh 投影信息
        """
        self._emit_log("info", f"🥽 [VR] 使用 android_vr 客户端解析: {url}")

        try:
            _ = locate_runtime_tool("yt-dlp.exe", "yt-dlp/yt-dlp.exe", "yt_dlp/yt-dlp.exe")
        except FileNotFoundError as e:
            raise FileNotFoundError(
                "未找到 yt-dlp.exe。请在设置页指定路径，或将 yt-dlp.exe 放入 _internal/yt-dlp/，或加入 PATH。"
            ) from e

        # 构建 android_vr 专用选项（不使用 cookies）
        vr_opts: dict[str, Any] = {
            # 同上：`quiet` / `no_warnings` 没有 CLI 映射，是死配置，已删。
            "ignoreerrors": False,
            "skip_download": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android_vr"],
                }
            },
        }

        # FFmpeg location
        ffmpeg_path = str(config_manager.get("ffmpeg_path") or "").strip()
        if ffmpeg_path and Path(ffmpeg_path).exists():
            vr_opts["ffmpeg_location"] = ffmpeg_path
        elif is_frozen():
            bundled_ffmpeg = find_bundled_executable("ffmpeg.exe", "ffmpeg/ffmpeg.exe")
            if bundled_ffmpeg is not None:
                vr_opts["ffmpeg_location"] = str(bundled_ffmpeg)

        # JS runtime
        self._maybe_configure_youtube_js_runtime(vr_opts)

        # === TTL 缓存查询（P2.1）===
        # vr_opts 里没有 cookiefile（android_vr 不支持），指纹自然退化为 "-"。
        _t_total = time.perf_counter()
        cache_key = self._parse_cache_key(url, "vr", vr_opts)
        cached = self._parse_cache_get(cache_key)
        if cached is not None:
            cached_info, age = cached
            self._log_parse_cache_hit(
                "vr", url, cached_info, age, (time.perf_counter() - _t_total) * 1000
            )
            return cached_info

        try:
            info = run_dump_single_json(
                url,
                vr_opts,
                extra_args=["--no-playlist"],
                cancel_event=cancel_event,
            )
            if info is None or info is False:
                raise RuntimeError("VR 解析失败：yt-dlp 未返回有效元数据。")
            if not isinstance(info, dict):
                raise RuntimeError(f"VR yt-dlp returned unexpected type: {type(info)!r}")

            info = cast(dict[str, Any], info)
            formats = info.get("formats") or []
            self._emit_log(
                "info",
                f"🥽 [VR] android_vr 解析完成: {len(formats)} 个格式",
            )

            # 统一注入 android_vr 可用格式 ID，供下游兼容性过滤使用。
            # 某些历史链路只在 merge 阶段写该字段，导致纯 VR 解析路径缺失元信息。
            try:
                android_vr_ids: list[str] = []
                for f in formats:
                    if not isinstance(f, dict):
                        continue
                    fid = str(f.get("format_id") or "")
                    if fid:
                        android_vr_ids.append(fid)
                if android_vr_ids:
                    info["__android_vr_format_ids"] = android_vr_ids
            except Exception:
                pass

            # 标记所有格式为 VR 来源
            info["__fluentytdl_vr_mode"] = True

            # 最高分辨率
            max_height = self._get_max_resolution(info)
            if max_height >= 4320:
                self._emit_log("info", f"🎉 [VR] 最高可用分辨率: {max_height}p (8K)")
            elif max_height >= 2160:
                self._emit_log("info", f"📺 [VR] 最高可用分辨率: {max_height}p (4K)")
            elif max_height > 0:
                self._emit_log("info", f"📺 [VR] 最高可用分辨率: {max_height}p")

            # VR 投影类型检测（逐格式标注 + 整体概览）
            self._detect_vr_projection(info)

            # 缓存写入必须放在全部后处理之后：存的是成品（含 __fluentytdl_vr_mode、
            # __android_vr_format_ids 与逐格式投影标注），否则命中时会缺投影信息。
            self._parse_cache_put(cache_key, info)

            return info
        except Exception as exc:
            if isinstance(exc, YtDlpCancelled):
                raise
            msg = str(exc)
            self._emit_log("error", f"🥽 [VR] 解析失败: {msg}")
            raise RuntimeError(f"VR 解析失败: {msg}") from exc

    async def extract_info(
        self, url: str, options: YoutubeServiceOptions | None = None
    ) -> dict[str, Any]:
        """Async metadata extraction (safe for UI thread)."""

        return await asyncio.to_thread(self.extract_info_sync, url, options)

    # --- Compatibility API (used by Phase 3 workers) ---
    def extract_video_info(
        self,
        url: str,
        options: YoutubeServiceOptions | None = None,
        *,
        read_cache: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Compatibility wrapper for older naming."""

        return self.extract_info_sync(
            url, options, read_cache=read_cache, cancel_event=cancel_event
        )

    def extract_playlist_flat(
        self,
        url: str,
        options: YoutubeServiceOptions | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Extract playlist entries in a lightweight (flat) way.

        This is designed for UI listing:
        - avoid per-entry format extraction
        - return entries quickly to reduce request bursts
        """

        options = options or YoutubeServiceOptions()
        base_opts = self.build_ydl_options(options)
        ydl_opts = dict(base_opts)

        # Key knobs to reduce requests
        ydl_opts.update(
            {
                "skip_download": True,
                "extract_flat": True,
                # prefer lightweight playlist enumeration
                "lazy_playlist": True,
                # keep errors explicit (UI will show)
                "ignoreerrors": False,
            }
        )

        if bool(config_manager.get("playlist_skip_authcheck") or False):
            ydl_opts = self._with_youtubetab_skip_authcheck(ydl_opts)

        self._emit_log("info", f"[PlaylistFlat] extracting: {url}")

        _t_total = time.perf_counter()
        cache_key = self._parse_cache_key(url, "playlist_flat", ydl_opts)
        cached = self._parse_cache_get(cache_key)
        if cached is not None:
            cached_info, age = cached
            self._log_parse_cache_hit(
                "playlist_flat", url, cached_info, age, (time.perf_counter() - _t_total) * 1000
            )
            return cached_info

        try:
            try:
                _ = locate_runtime_tool("yt-dlp.exe", "yt-dlp/yt-dlp.exe", "yt_dlp/yt-dlp.exe")
            except FileNotFoundError as e:
                raise FileNotFoundError(
                    "未找到 yt-dlp.exe。请在设置页指定路径，或将 yt-dlp.exe 放入 _internal/yt-dlp/，或加入 PATH。"
                ) from e

            try:
                info = run_dump_single_json(
                    url,
                    ydl_opts,
                    extra_args=["--flat-playlist", "--lazy-playlist"],
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                if isinstance(exc, YtDlpCancelled):
                    raise
                msg = str(exc)
                if self._should_retry_with_youtubetab_skip_authcheck(msg):
                    retry_opts = self._with_youtubetab_skip_authcheck(ydl_opts)
                    if retry_opts is not ydl_opts:
                        self._emit_log(
                            "warning",
                            "检测到播放列表 authcheck 限制提示，按 yt-dlp 官方建议自动启用 youtubetab:skip=authcheck 并重试一次。",
                        )
                        info = run_dump_single_json(
                            url,
                            retry_opts,
                            extra_args=["--flat-playlist", "--lazy-playlist"],
                            cancel_event=cancel_event,
                        )
                    else:
                        raise
                else:
                    fallback_info = self._handle_channel_tab_fallback(
                        url, msg, ydl_opts, ["--flat-playlist", "--lazy-playlist"], cancel_event
                    )
                    if fallback_info:
                        info = fallback_info
                    else:
                        raise

            if not isinstance(info, dict):
                raise RuntimeError("播放列表解析失败：返回结果为空")

            info = cast(dict[str, Any], info)
            self._extend_playlist_entries_from_youtube_continuations(url, info)
            total_ms = (time.perf_counter() - _t_total) * 1000
            self._parse_cache_put(cache_key, info)
            self._emit_log(
                "info",
                f"[PlaylistFlat] 解析完成: {url} 耗时 {total_ms / 1000:.2f}s "
                f"(entries={len(info.get('entries') or [])})",
            )
            return info
        except Exception as exc:
            msg = str(exc)
            total_ms = (time.perf_counter() - _t_total) * 1000
            self._emit_log("error", f"播放列表解析失败 (耗时 {total_ms / 1000:.2f}s): {msg}")
            raise

    def _extend_playlist_entries_from_youtube_continuations(
        self, url: str, info: dict[str, Any]
    ) -> None:
        entries = info.get("entries")
        if not isinstance(entries, list):
            return
        try:
            playlist_count = int(info.get("playlist_count") or 0)
        except Exception:
            playlist_count = 0
        if playlist_count <= len(entries):
            return

        existing_ids = {str(entry.get("id") or "") for entry in entries if isinstance(entry, dict)}
        try:
            extra = self._fetch_youtube_playlist_continuation_entries(
                url, existing_ids=existing_ids
            )
        except Exception as exc:
            self._emit_log("warning", f"[PlaylistFlat] continuation fallback failed: {exc}")
            return

        appended = 0
        seen = set(existing_ids)
        for entry in extra:
            vid = str(entry.get("id") or "")
            if not vid or vid in seen:
                continue
            entries.append(entry)
            seen.add(vid)
            appended += 1

        if appended:
            self._emit_log(
                "info",
                f"[PlaylistFlat] continuation fallback appended {appended} entries "
                f"({len(entries)}/{playlist_count})",
            )

    def _fetch_youtube_playlist_continuation_entries(
        self, url: str, *, existing_ids: set[str]
    ) -> list[dict[str, Any]]:
        playlist_id = self._extract_playlist_id(url)
        if not playlist_id:
            return []

        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
        session = requests.Session()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            )
        }
        response = session.get(playlist_url, headers=headers, timeout=20)
        response.raise_for_status()
        html = response.text

        key_match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        version_match = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        data_match = re.search(r"var ytInitialData = (\{.*?\});</script>", html)
        if not (key_match and version_match and data_match):
            return []

        data = json.loads(data_match.group(1))
        tokens = self._find_youtube_continuation_tokens(data)
        api_key = key_match.group(1)
        client_version = version_match.group(1)

        entries: list[dict[str, Any]] = []
        seen = set(existing_ids)
        queue = list(tokens)
        visited: set[str] = set()

        while queue:
            token = queue.pop(0)
            if not token or token in visited:
                continue
            visited.add(token)
            body = {
                "context": {"client": {"clientName": "WEB", "clientVersion": client_version}},
                "continuation": token,
            }
            page = session.post(
                f"https://www.youtube.com/youtubei/v1/browse?key={api_key}",
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Origin": "https://www.youtube.com",
                    "Referer": playlist_url,
                },
                json=body,
                timeout=20,
            )
            page.raise_for_status()
            payload = page.json()

            for lockup in self._find_lockup_view_models(payload):
                entry = self._entry_from_lockup_view_model(lockup, playlist_id)
                vid = str(entry.get("id") or "")
                if vid and vid not in seen:
                    entries.append(entry)
                    seen.add(vid)

            for next_token in self._find_youtube_continuation_tokens(payload):
                if next_token not in visited and next_token not in queue:
                    queue.append(next_token)

        return entries

    @staticmethod
    def _extract_playlist_id(url: str) -> str:
        parsed = urlparse(url)
        list_values = parse_qs(parsed.query).get("list")
        if list_values and list_values[0]:
            return list_values[0]
        if url.startswith("PL"):
            return url.split("&", 1)[0]
        return ""

    @staticmethod
    def _find_youtube_continuation_tokens(data: Any) -> list[str]:
        tokens: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                command = node.get("continuationCommand")
                if isinstance(command, dict):
                    token = str(command.get("token") or "")
                    if token and token not in tokens:
                        tokens.append(token)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data)
        return tokens

    @staticmethod
    def _find_lockup_view_models(data: Any) -> list[dict[str, Any]]:
        lockups: list[dict[str, Any]] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                lockup = node.get("lockupViewModel")
                if isinstance(lockup, dict):
                    lockups.append(lockup)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data)
        return lockups

    @staticmethod
    def _entry_from_lockup_view_model(lockup: dict[str, Any], playlist_id: str) -> dict[str, Any]:
        metadata = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
        title = str(metadata.get("title", {}).get("content") or "")
        video_id = ""
        duration = None
        thumbnail = ""

        def walk(node: Any) -> None:
            nonlocal video_id, duration, thumbnail
            if isinstance(node, dict):
                watch = node.get("watchEndpoint")
                if isinstance(watch, dict) and not video_id:
                    video_id = str(watch.get("videoId") or "")
                url_endpoint = node.get("urlEndpoint")
                if isinstance(url_endpoint, dict) and not video_id:
                    parsed = urlparse(str(url_endpoint.get("url") or ""))
                    video_id = (parse_qs(parsed.query).get("v") or [""])[0]
                badge = node.get("thumbnailBadgeViewModel")
                if isinstance(badge, dict) and duration is None:
                    duration = YoutubeService._parse_duration_text(str(badge.get("text") or ""))
                sources = node.get("sources")
                if isinstance(sources, list) and sources:
                    best = max(
                        [source for source in sources if isinstance(source, dict)],
                        key=lambda source: int(source.get("width") or 0),
                        default={},
                    )
                    if best.get("url"):
                        thumbnail = str(best.get("url"))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(lockup)
        return {
            "_type": "url",
            "id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}",
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}",
            "title": title or video_id,
            "duration": duration,
            "thumbnail": thumbnail,
            "thumbnails": [{"url": thumbnail}] if thumbnail else [],
        }

    @staticmethod
    def _parse_duration_text(value: str) -> int | None:
        parts = [part for part in value.strip().split(":") if part.isdigit()]
        if not parts:
            return None
        total = 0
        for part in parts:
            total = total * 60 + int(part)
        return total

    @staticmethod
    def _should_retry_with_youtubetab_skip_authcheck(error_text: str) -> bool:
        """Detect yt-dlp's official hint for playlist authcheck.

        yt-dlp error usually contains: "pass --extractor-args youtubetab:skip=authcheck".
        """

        lower = (error_text or "").lower()
        return "youtubetab:skip=authcheck" in lower or (
            "authcheck" in lower and "youtubetab" in lower
        )

    @staticmethod
    def _is_auth_blocked_error(message: str) -> bool:
        """判断是否为认证/风控类错误"""
        lower = message.lower()
        return any(
            kw in lower
            for kw in (
                "sign in to confirm",
                "not a bot",
                "login required",
                "http error 403",
                "forbidden",
            )
        )

    @staticmethod
    def _with_youtubetab_skip_authcheck(ydl_opts: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ydl_opts with extractor-args youtubetab:skip=authcheck injected.

        If already present, returns the original dict (to avoid infinite retries).
        """

        extractor_args = ydl_opts.get("extractor_args")
        if extractor_args is None:
            new_opts = dict(ydl_opts)
            new_opts["extractor_args"] = {"youtubetab": {"skip": ["authcheck"]}}
            return new_opts

        if not isinstance(extractor_args, dict):
            # Unrecognized format; do not try to mutate.
            return ydl_opts

        youtubetab_args = extractor_args.get("youtubetab")
        if isinstance(youtubetab_args, dict):
            existing = youtubetab_args.get("skip")
            if isinstance(existing, (list, tuple)) and any(
                str(x).strip().lower() == "authcheck" for x in existing
            ):
                return ydl_opts
            if isinstance(existing, str) and "authcheck" in existing.lower():
                return ydl_opts

        # Copy-on-write to avoid mutating callers unexpectedly.
        new_opts = dict(ydl_opts)
        new_extractor_args = dict(extractor_args)
        new_youtubetab_args: dict[str, Any] = {}
        if isinstance(youtubetab_args, dict):
            new_youtubetab_args.update(youtubetab_args)
        new_youtubetab_args["skip"] = ["authcheck"]
        new_extractor_args["youtubetab"] = new_youtubetab_args
        new_opts["extractor_args"] = new_extractor_args
        return new_opts

    def get_local_version(self) -> str:
        return run_version()

    # ── 频道解析 ───────────────────────────────────────────────────────────

    def extract_channel_flat(
        self,
        url: str,
        options: YoutubeServiceOptions | None = None,
        *,
        tab: str = "videos",
        base_ydl_opts: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """频道单个标签页的 flat 提取（带 channel_tab TTL 缓存）。

        在 extract_playlist_flat 基础上增加：
        - 根据 tab 参数自动拼接 URL 后缀 (/videos /shorts /streams)
        - streams 标签页过滤掉正在直播的条目

        `base_ydl_opts`：调用方已构建好的基础选项。ChannelExtractWorker 会用三个线程
        并发拉三个标签页，共享同一份 opts 可以避免重复读 cookie / 重复探测 exe——
        传入时本方法不再调用 build_ydl_options。

        排序不在这里做：`--playlist-reverse` 与 `--lazy-playlist` 语义相冲（要倒序就得
        先拿全），且每切一次排序都要重跑子进程。UI 侧对 entries 本地反转即可，
        reverse 因此也不必进缓存键。
        """
        normalized_url = self._normalize_channel_url(url, tab)

        if base_ydl_opts is not None:
            ydl_opts = dict(base_ydl_opts)
        else:
            ydl_opts = dict(self.build_ydl_options(options or YoutubeServiceOptions()))
        ydl_opts.update(
            {
                "skip_download": True,
                "extract_flat": True,
                "lazy_playlist": True,
                "ignoreerrors": False,
            }
        )

        if bool(config_manager.get("playlist_skip_authcheck") or False):
            ydl_opts = self._with_youtubetab_skip_authcheck(ydl_opts)

        extra_args = ["--flat-playlist", "--lazy-playlist"]
        if tab == "streams":
            # 直播中的条目没有可下载的完整文件，列出来只会让用户点了报错
            extra_args.extend(["--match-filter", "is_live != True"])

        self._emit_log("info", f"[ChannelFlat] extracting: {normalized_url} (tab={tab})")

        # === TTL 缓存查询（P2.2）===
        # tab 已经在 normalized_url 后缀里，键天然区分三个标签页，无需额外字段。
        _t_total = time.perf_counter()
        cache_key = self._parse_cache_key(normalized_url, "channel_tab", ydl_opts)
        cached = self._parse_cache_get(cache_key)
        if cached is not None:
            cached_info, age = cached
            cached_info["__fluentytdl_tab"] = tab
            self._log_parse_cache_hit(
                "channel_tab",
                normalized_url,
                cached_info,
                age,
                (time.perf_counter() - _t_total) * 1000,
            )
            return cached_info

        try:
            try:
                _ = locate_runtime_tool("yt-dlp.exe", "yt-dlp/yt-dlp.exe", "yt_dlp/yt-dlp.exe")
            except FileNotFoundError as e:
                raise FileNotFoundError(
                    "未找到 yt-dlp.exe。请在设置页指定路径，或将 yt-dlp.exe 放入 _internal/yt-dlp/，或加入 PATH。"
                ) from e

            try:
                info = run_dump_single_json(
                    normalized_url,
                    ydl_opts,
                    extra_args=extra_args,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                if isinstance(exc, YtDlpCancelled):
                    raise
                msg = str(exc)
                if self._should_retry_with_youtubetab_skip_authcheck(msg):
                    retry_opts = self._with_youtubetab_skip_authcheck(ydl_opts)
                    if retry_opts is not ydl_opts:
                        self._emit_log(
                            "warning",
                            "检测到频道 authcheck 限制，自动启用 youtubetab:skip=authcheck 并重试。",
                        )
                        info = run_dump_single_json(
                            normalized_url,
                            retry_opts,
                            extra_args=extra_args,
                            cancel_event=cancel_event,
                        )
                    else:
                        raise
                else:
                    fallback_info = self._handle_channel_tab_fallback(
                        normalized_url, msg, ydl_opts, extra_args, cancel_event
                    )
                    if fallback_info:
                        info = fallback_info
                    else:
                        raise

            if not isinstance(info, dict):
                raise RuntimeError("频道解析失败：返回结果为空")

            info = cast(dict[str, Any], info)
            # 在 put 之前打标记：缓存里存的应当是成品，命中时不需要调用方补写
            info["__fluentytdl_tab"] = tab
            total_ms = (time.perf_counter() - _t_total) * 1000
            self._parse_cache_put(cache_key, info)
            self._emit_log(
                "info",
                f"[ChannelFlat] 解析完成: {normalized_url} 耗时 {total_ms / 1000:.2f}s "
                f"(tab={tab}, entries={len(info.get('entries') or [])})",
            )
            return info
        except Exception as exc:
            msg = str(exc)
            total_ms = (time.perf_counter() - _t_total) * 1000
            self._emit_log("error", f"频道解析失败 (耗时 {total_ms / 1000:.2f}s): {msg}")
            raise

    @staticmethod
    def _normalize_channel_url(url: str, tab: str) -> str:
        """根据标签页选择规范化频道 URL 后缀"""
        url = url.rstrip("/")
        # 移除已有的标签页后缀
        for suffix in ("/videos", "/shorts", "/streams", "/playlists", "/community"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        # 拼接目标标签页
        if tab == "all":
            return url  # 返回基础URL给ChannelExtractWorker处理
        if tab in ("videos", "shorts", "streams"):
            return f"{url}/{tab}"
        return f"{url}/videos"  # 默认回退

    def _handle_channel_tab_fallback(
        self,
        url: str,
        exc_msg: str,
        opts: dict[str, Any],
        extra_args: list[str],
        cancel_event: threading.Event | None,
    ) -> dict[str, Any] | None:
        """如果遇到频道缺少某个标签页的错误，自动尝试回退到其他标签页"""
        msg_lower = exc_msg.lower()
        if "does not have a" not in msg_lower or "tab" not in msg_lower:
            return None

        tabs_to_try = []
        if url.endswith("/videos"):
            tabs_to_try = ["/shorts", "/streams", "/playlists", "/releases", "/podcasts"]
            base_url = url[:-7]
        elif url.endswith("/shorts"):
            tabs_to_try = ["/videos", "/streams", "/playlists", "/releases", "/podcasts"]
            base_url = url[:-7]
        elif url.endswith("/streams"):
            tabs_to_try = ["/videos", "/shorts", "/playlists", "/releases", "/podcasts"]
            base_url = url[:-8]
        else:
            return None

        for fallback_tab in tabs_to_try:
            if cancel_event and cancel_event.is_set():
                break

            new_url = base_url + fallback_tab
            self._emit_log("warning", f"频道当前标签页不存在，自动尝试回退解析: {fallback_tab} ...")
            try:
                info = run_dump_single_json(
                    new_url, opts, extra_args=extra_args, cancel_event=cancel_event
                )
                if info:
                    self._emit_log("info", f"✅ 回退解析成功: {fallback_tab}")
                    return cast(dict[str, Any], info)
            except Exception as e:
                if isinstance(e, YtDlpCancelled):
                    raise
                new_msg = str(e).lower()
                if "does not have a" in new_msg and "tab" in new_msg:
                    continue  # 尝试下一个
                raise  # 其他错误直接抛出

        return None


youtube_service = YoutubeService()
