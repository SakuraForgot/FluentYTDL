"""
FluentYTDL 统一身份验证服务

重构后的核心设计:
- 所有 Cookie 处理统一走此服务
- 用户在 UI 选择"来源"，底层全部转为文件路径
- 彻底避免 yt-dlp --cookies-from-browser 的文件锁问题

架构:
  UI (选浏览器/文件) -> AuthService -> rookiepy/文件读取 -> 临时 cookies.txt -> yt-dlp --cookies
"""

from __future__ import annotations

import ctypes
import json
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text

from ..core.config_manager import config_manager
from ..utils.logger import logger
from ..utils.message_catalog import english
from .cookie_cleaner import CookieCleaner

# 尝试导入 rookiepy
try:
    import rookiepy

    HAS_ROOKIEPY = True
except ImportError:
    rookiepy = None
    HAS_ROOKIEPY = False
    log_text(logger, "warning", "rookiepy 未安装，浏览器 Cookie 自动提取功能不可用")


# ==================== Windows 管理员权限检查 ====================


def is_admin() -> bool:
    """检查当前是否为管理员权限"""
    if sys.platform != "win32":
        return True  # 非 Windows 假设无需提权
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _is_appbound_error(error: Exception) -> bool:
    """检测是否为 Chromium App-Bound 加密错误（需要管理员权限）"""
    err_str = str(error).lower()
    return (
        "admin" in err_str
        or "appbound" in err_str
        or "v130" in err_str
        or "decrypted only when running as admin" in err_str
    )


class AuthSourceType(str, Enum):
    """验证源类型"""

    NONE = "none"  # 不使用身份验证
    # Chromium 内核浏览器（v130+ 需要管理员权限）
    EDGE = "edge"  # Microsoft Edge
    CHROME = "chrome"  # Google Chrome —— 已停止支持，见 LEGACY_SOURCES
    CHROMIUM = "chromium"  # Chromium
    BRAVE = "brave"  # Brave
    OPERA = "opera"  # Opera
    OPERA_GX = "opera_gx"  # Opera GX
    VIVALDI = "vivaldi"  # Vivaldi
    ARC = "arc"  # Arc
    # Firefox 内核浏览器（无需管理员权限）
    FIREFOX = "firefox"  # Firefox
    LIBREWOLF = "librewolf"  # LibreWolf
    # 其它第三方定制
    CENT = "centbrowser"  # 百分浏览器 —— 已停止支持，见 LEGACY_SOURCES
    # WebView2 登录获取
    WEBVIEW2 = "webview2"  # 动态本地注入登录
    # 其他
    FILE = "file"  # 手动导入的 cookies.txt


#: 已停止支持的提取源。**枚举值故意保留**：老配置里存着 `"chrome"` / `"centbrowser"`，
#: 删掉成员会让 `AuthSourceType(source_value)` 直接抛 ValueError，用户升级后启动即报错。
#: 保留成员 + `_load_config()` 里一次性迁到 EDGE，是唯一不炸老配置的删除方式。
#:
#: - Chrome：v127+ 的 App-Bound Encryption 拒绝任何非本进程解密，rookiepy 在管理员
#:   权限下也拉不出来，留着只会让用户以为"选了就能用"。
#: - 百分浏览器：靠一份 110 行的 DPAPI + AES-GCM 自建提取器硬扛，维护成本与收益完全
#:   不成比例，已随本次改动删除。
LEGACY_SOURCES = {AuthSourceType.CHROME, AuthSourceType.CENT}


#: 浏览器下拉框的**唯一**顺序来源：`(AuthSourceType, 展示名)`，索引即下拉框索引。
#:
#: 以前这份顺序在 UI 层被手抄了五份（`settings_page` 四处 + `download_config_window` 一处），
#: 每份都是写死 11 项的位置列表。加减一个浏览器要同步改五处，漏一处就会整体错位 ——
#: 用户选 "Firefox"、程序实际去提取 Arc，而且不报任何错。删 Chrome / 百分浏览器正是这种
#: 改动，所以先把五份收敛成一份，再删。
#:
#: 浏览器名都是专名（Microsoft Edge、Brave……），不进 `self.tr()`。
BROWSER_COMBO_ITEMS: list[tuple[AuthSourceType, str]] = [
    (AuthSourceType.EDGE, "Microsoft Edge"),
    (AuthSourceType.CHROMIUM, "Chromium"),
    (AuthSourceType.BRAVE, "Brave"),
    (AuthSourceType.OPERA, "Opera"),
    (AuthSourceType.OPERA_GX, "Opera GX"),
    (AuthSourceType.VIVALDI, "Vivaldi"),
    (AuthSourceType.ARC, "Arc"),
    (AuthSourceType.FIREFOX, "Firefox"),
    (AuthSourceType.LIBREWOLF, "LibreWolf"),
]

#: 浏览器类型列表（用于逻辑判断）。**由下拉框列表派生**，保证"下拉框里选得到"和
#: "代码认它是浏览器源"永远是同一集合。
BROWSER_SOURCES = [source for source, _ in BROWSER_COMBO_ITEMS]

#: 展示名列表，直接喂给 ComboBox.addItems()
BROWSER_COMBO_LABELS = [label for _, label in BROWSER_COMBO_ITEMS]


def browser_source_at(index: int) -> AuthSourceType:
    """下拉框索引 → 提取源。越界一律回落 Edge，不抛异常。"""
    if 0 <= index < len(BROWSER_COMBO_ITEMS):
        return BROWSER_COMBO_ITEMS[index][0]
    return AuthSourceType.EDGE


def browser_combo_index(source: AuthSourceType) -> int:
    """提取源 → 下拉框索引。不在列表里（含 LEGACY_SOURCES）一律回 0（Edge）。"""
    for i, (candidate, _) in enumerate(BROWSER_COMBO_ITEMS):
        if candidate is source:
            return i
    return 0


# 需要管理员权限的浏览器（Chromium 内核 v130+）
ADMIN_REQUIRED_BROWSERS = [
    AuthSourceType.EDGE,
    AuthSourceType.CHROMIUM,
    AuthSourceType.BRAVE,
    AuthSourceType.OPERA,
    AuthSourceType.OPERA_GX,
    AuthSourceType.VIVALDI,
    AuthSourceType.ARC,
]

# 各平台需要的 Cookie 域名
PLATFORM_DOMAINS = {
    "youtube": [".youtube.com", ".google.com"],
    "bilibili": [".bilibili.com"],
    "twitter": [".twitter.com", ".x.com"],
}

# YouTube 登录验证所需的关键 Cookie
YOUTUBE_REQUIRED_COOKIES = {"SID", "HSID", "SSID", "SAPISID", "APISID"}

# 提交闸门专用的认证态 marker（见 `_validate_cookies` 的 youtube 分支）。yt-dlp 的
# `--cookies` 回写会用「已登出」jar 覆盖真相源，而 SID 家族在 `.google.com` 上能挺过回写
# ——于是一个「只剩 .google.com SID、无任何 .youtube.com 登录态」的半 jar 仍能骗过下面
# 那套 name-only 检查（日志里 `Valid=True`，yt-dlp 却判未登录）。故在 youtube 分支额外要求：
# **至少一个 marker 落在 `.youtube.com` 域上**。这不是新的「真理」、也不是登录活性证明，
# 只是挡住「明显退化成访客态」的候选把一个可用文件覆盖掉。访客态 Cookie（VISITOR_INFO1_LIVE
# / PREF / __Secure-ROLLOUT_TOKEN / __Secure-YNID）一律不算。
YOUTUBE_AUTH_MARKERS = {"LOGIN_INFO", "SID", "SAPISID", "__Secure-1PSID"}

# X (Twitter) 登录验证关键 Cookie (仅做存在性检查)
X_REQUIRED_COOKIES = {"auth_token", "ct0"}

# Cookie 子系统的两个真相源对应的展示名。都是专名，无需 tr()。
# 单点定义，避免各处再手写 `"YouTube" if platform == "youtube" else "X (Twitter)"`。
PLATFORM_LABELS = {
    "youtube": "YouTube",
    "twitter": "X (Twitter)",
}


@dataclass
class AuthStatus:
    """验证状态"""

    valid: bool = False
    message: str = field(default_factory=lambda: tr_text("未验证"))
    cookie_count: int = 0
    last_updated: str | None = None
    account_hint: str | None = None  # 账户提示 (如 "YouTube Premium")


@dataclass
class AuthProfile:
    """
    认证配置（用于高级多账户管理）
    """

    name: str  # 显示名称
    platform: str = "youtube"  # 平台标识
    source_type: AuthSourceType = AuthSourceType.EDGE
    file_path: str | None = None  # 当 FILE 类型使用
    cached_cookie_path: str | None = None  # 缓存的 cookie 文件路径
    enabled: bool = True
    last_updated: str | None = None
    cookie_count: int = 0
    is_valid: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["source_type"] = self.source_type.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuthProfile:
        if "source_type" in data:
            data["source_type"] = AuthSourceType(data["source_type"])
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class WebView2Account:
    """WebView2 多账号配置"""

    account_id: str
    display_name: str
    platform: str = "youtube"
    profile_dir: str = ""
    cached_cookie_path: str = ""
    last_extracted_at: str | None = None
    cookie_count: int = 0
    valid: bool = False
    is_default: bool = False
    notes: str | None = None
    # 该账号是否被 YouTube 拉进 SABR-only 灰度：一旦解析输出里出现 "forcing SABR
    # streaming" / "formats ... missing a url"，就在账号上打标并持久化。SABR 是
    # 账号级实验，命中后该账号后续所有视频都要追加 web_safari 客户端才能拿到高清直链。
    # 旧 accounts.json 无此键 → from_dict 的 known-fields 过滤令其默认 False。
    sabr_only: bool = False
    # None: legacy record; empty: user-chosen name; otherwise a stable built-in name ID.
    builtin_name: str | None = None

    @property
    def localized_name(self) -> str:
        platform_name = (
            "YouTube"
            if self.platform == "youtube"
            else "X"
            if self.platform == "x"
            else self.platform
        )
        kind = self.builtin_name
        if kind is None:
            # Exact legacy aliases only: never replace part of a user-defined name.
            if self.display_name in (
                QT_TRANSLATE_NOOP("WebView2Account", "默认账号"),
                "Default",
                "Default Account",
            ):
                kind = "default"
            elif self.display_name in (
                QT_TRANSLATE_NOOP("RuntimeText", "{0} 默认账号").format(platform_name),
                f"{platform_name} Default Account",
                english("{0} 默认账号", platform_name),
            ):
                kind = "platform_default"
            elif self.display_name in (
                QT_TRANSLATE_NOOP("RuntimeText", "未命名账号"),
                "Unnamed Account",
                english("未命名账号"),
            ):
                kind = "unnamed"
        if kind == "default":
            return QCoreApplication.translate("WebView2Account", "默认账号")
        if kind == "platform_default":
            return tr_text("{0} 默认账号", platform_name)
        if kind == "unnamed":
            return tr_text("未命名账号")
        return self.display_name

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebView2Account:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


class AuthService:
    """
    统一身份验证服务

    核心职责:
    1. 管理当前激活的验证源
    2. 按需提取/读取 Cookie 并生成临时文件
    3. 向 yt-dlp 提供统一的 cookie 文件路径
    """

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or Path(tempfile.gettempdir()) / "fluentytdl_auth"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # 统一运行目录下的 bin（与 cookie_sentinel 保持一致）
        self._runtime_bin_dir = self._resolve_runtime_bin_dir()

        self._config_path = self.cache_dir / "auth_config.json"
        self._profiles_path = self.cache_dir / "profiles.json"
        self._webview2_accounts_dir = self._runtime_bin_dir / "dle_user"
        self._webview2_accounts_path = self._webview2_accounts_dir / "accounts.json"
        self._webview2_accounts_dir.mkdir(parents=True, exist_ok=True)

        # 当前配置
        self._current_source: AuthSourceType = AuthSourceType.NONE
        self._current_file_path: str | None = None
        self._auto_refresh: bool = True
        self._last_status: AuthStatus = AuthStatus()
        self._current_webview2_account_ids: dict[str, str] = {}

        # 无登录态（无当前 WebView2 账号）时 SABR 标记的退化载体：进程内 sticky，
        # 重启清零。有当前账号时一律以账号对象上的 `sabr_only` 为准（持久化）。
        self._session_sabr_only: bool = False

        # 高级：多账户配置
        self._profiles: dict[str, AuthProfile] = {}
        self._webview2_accounts: dict[str, WebView2Account] = {}

        self._load_config()
        self._load_webview2_accounts()
        self._migrate_legacy_webview2_cache_if_needed()
        self._migrate_accounts_to_platform_isolated_dirs()
        self._ensure_current_webview2_account_valid("youtube")
        self._ensure_current_webview2_account_valid("twitter")

    # ==================== 属性 ====================

    @property
    def available(self) -> bool:
        """rookiepy 是否可用"""
        return HAS_ROOKIEPY

    @property
    def current_source(self) -> AuthSourceType:
        """当前验证源"""
        return self._current_source

    @property
    def current_source_display(self) -> str:
        """当前验证源的显示名称"""

        names = {
            AuthSourceType.NONE: tr_text("未启用"),
            AuthSourceType.EDGE: tr_text("Edge 浏览器"),
            AuthSourceType.CHROME: tr_text("Chrome 浏览器"),
            AuthSourceType.CHROMIUM: tr_text("Chromium 浏览器"),
            AuthSourceType.BRAVE: tr_text("Brave 浏览器"),
            AuthSourceType.OPERA: tr_text("Opera 浏览器"),
            AuthSourceType.OPERA_GX: tr_text("Opera GX 浏览器"),
            AuthSourceType.VIVALDI: tr_text("Vivaldi 浏览器"),
            AuthSourceType.ARC: tr_text("Arc 浏览器"),
            AuthSourceType.FIREFOX: tr_text("Firefox 浏览器"),
            AuthSourceType.LIBREWOLF: tr_text("LibreWolf 浏览器"),
            AuthSourceType.CENT: tr_text("百分浏览器 (Cent)"),
            AuthSourceType.WEBVIEW2: tr_text("登录获取 (推荐)"),
            AuthSourceType.FILE: tr_text("手动导入文件"),
        }
        return names.get(self._current_source, tr_text("未知"))

    @property
    def auto_refresh(self) -> bool:
        """是否自动刷新 Cookie"""
        return self._auto_refresh

    @property
    def last_status(self) -> AuthStatus:
        """最近一次验证状态"""
        return self._last_status

    @property
    def current_webview2_account_id(self) -> str | None:
        """当前激活的 YouTube WebView2 账号 ID (向下兼容)"""
        return self._current_webview2_account_ids.get("youtube")

    @property
    def current_webview2_account(self) -> WebView2Account | None:
        """当前激活的 YouTube WebView2 账号 (向下兼容)"""
        return self.get_current_webview2_account("youtube")

    def get_current_webview2_account_id(self, platform: str) -> str | None:
        """获取指定平台的当前激活 WebView2 账号 ID"""
        return self._current_webview2_account_ids.get(platform)

    def get_current_webview2_account(self, platform: str) -> WebView2Account | None:
        """获取指定平台的当前激活 WebView2 账号"""
        account_id = self.get_current_webview2_account_id(platform)
        if not account_id:
            return None
        return self._webview2_accounts.get(account_id)

    # ==================== SABR-only 账号级标记 ====================
    # SABR 是 YouTube 的账号级灰度：命中后带下载链接的高清格式被丢弃，只有追加
    # web_safari 客户端才能拿回直链。标记须落在 build_ydl_options() 之外的共享处，
    # 因为解析与下载各自独立调 build_ydl_options()，必须让两条路读到同一个状态。

    def get_youtube_sabr_only(self) -> bool:
        """当前 YouTube 账号是否处于 SABR-only 灰度。

        有当前账号 → 以账号对象上的持久化 `sabr_only` 为准（跨重启、切账号隔离）；
        无当前账号（无登录态）→ 退化到进程内 sticky 标志。
        """
        account = self.get_current_webview2_account("youtube")
        if account is not None:
            return bool(account.sabr_only)
        return self._session_sabr_only

    def mark_youtube_sabr_only(self) -> None:
        """把当前 YouTube 账号（或无账号时的会话）标记为 SABR-only。

        幂等：已为 True 直接返回，绝不重复写盘。有当前账号时持久化到 accounts.json，
        无账号时只置内存 sticky（重启清零）。
        """
        account = self.get_current_webview2_account("youtube")
        if account is not None:
            if account.sabr_only:
                return
            account.sabr_only = True
            self._save_webview2_accounts()
            return
        self._session_sabr_only = True

    # ==================== 核心方法 ====================

    def set_source(
        self,
        source: AuthSourceType,
        file_path: str | None = None,
        auto_refresh: bool = True,
    ) -> None:
        """
        设置验证源

        当来源变化时，会通知 CookieSentinel 清理旧的 Cookie 文件，
        确保不会复用来自不同浏览器的旧 Cookie。

        Args:
            source: 验证源类型
            file_path: 当 FILE 类型需要
            auto_refresh: 是否自动刷新
        """
        old_source = self._current_source

        self._current_source = source
        self._current_file_path = file_path if source == AuthSourceType.FILE else None
        self._auto_refresh = auto_refresh
        self._save_config()

        log_text(logger, "info", "验证源已设置: {0}", self.current_source_display)

        # 注意：不再立即清理旧 Cookie 文件
        # 延迟到实际提取成功后再清理，避免提取失败时丢失旧 Cookie
        if old_source != source:
            log_text(logger, "info", "验证源变化: {0} -> {1}", old_source.value, source.value)
            log_text(logger, "info", "将在下次成功提取后更新 Cookie 文件")

    def get_cookie_file_for_ytdlp(
        self,
        platform: str = "youtube",
        force_refresh: bool = False,
    ) -> str | None:
        """
        获取 yt-dlp 可用的 cookie 文件路径

        这是被 yt_dlp_cli.py 调用的核心方法。

        Args:
            platform: 平台标识
            force_refresh: 强制刷新（忽略缓存）

        Returns:
            cookie 文件的绝对路径，或 None（未启用验证）
        """
        if self._current_source == AuthSourceType.NONE:
            return None

        try:
            if self._current_source == AuthSourceType.FILE:
                # 手动导入的文件：读取 -> 清洗 -> 缓存 -> 返回缓存路径
                if self._current_file_path and Path(self._current_file_path).exists():
                    cache_file = self.cache_dir / f"cached_file_{platform}.txt"
                    src_path = Path(self._current_file_path)

                    # 检查缓存是否存在且比源文件新
                    need_refresh = force_refresh or not cache_file.exists()
                    if not need_refresh:
                        src_mtime = src_path.stat().st_mtime
                        cache_mtime = cache_file.stat().st_mtime
                        if src_mtime > cache_mtime:
                            need_refresh = True

                    if need_refresh:
                        # 读取原生导入的 Cookie
                        content = src_path.read_text(encoding="utf-8", errors="replace")
                        cookies = self._parse_netscape_cookies(content)
                        original_count = len(cookies)

                        cookies = CookieCleaner.clean(
                            cookies, platform, config_manager.get("cookie_cleaning_enabled", True)
                        )
                        cleaned_count = len(cookies)
                        if original_count != cleaned_count:
                            log_text(
                                logger,
                                "info",
                                "[{0}] 导入文件 Cookie 清洗完成: {1} -> {2} (移除: {3})",
                                platform,
                                original_count,
                                cleaned_count,
                                original_count - cleaned_count,
                            )
                        else:
                            log_text(
                                logger,
                                "info",
                                "[{0}] 导入文件 Cookie 清洗完成: 保持 {1} 个",
                                platform,
                                original_count,
                            )

                        # 写入特殊的缓存文件
                        self._write_netscape_file(cookies, cache_file)

                    self._update_status_from_file(str(cache_file))
                    return str(cache_file)
                else:
                    self._last_status = AuthStatus(
                        valid=False,
                        message=tr_text("Cookie 文件不存在"),
                    )
                    return None

            elif self._current_source == AuthSourceType.WEBVIEW2:
                # WebView2 登录获取：使用动态插件提取
                cache_file = self._get_webview2_cache_file(platform)

                # WebView2 是交互式流程，仅在用户显式点击刷新 (force_refresh=True) 时才启动浏览器
                # 其他场景（启动同步、下载前检查）只使用已有缓存
                if force_refresh:
                    try:
                        from .providers.webview2_provider import WebView2CookieProvider

                        account = self.get_current_webview2_account(platform)
                        profile_dir = account.profile_dir if account else None
                        account_label = account.localized_name if account else "default"
                        profile_has_data = False
                        if profile_dir:
                            try:
                                profile_has_data = Path(profile_dir).exists() and any(
                                    Path(profile_dir).iterdir()
                                )
                            except Exception:
                                profile_has_data = False

                        log_text(
                            logger, "info", "开始 WebView2 登录流程（账号: {0}）...", account_label
                        )
                        provider = WebView2CookieProvider()
                        cookies = provider.extract_cookies(
                            platform=platform,
                            storage_path=profile_dir,
                            session_tag=account_label,
                            start_hidden=profile_has_data,
                            reveal_after_seconds=8,
                        )

                        if cookies is None:
                            raise RuntimeError(
                                provider.get_last_error().get("error")
                                or tr_text("登录失败，未返回 Cookie 数据")
                            )

                        # 清洗 Cookie（合规过滤）
                        cookies_dicts = []
                        for c in cookies:
                            cookies_dicts.append(
                                {
                                    "domain": c.get("domain", ""),
                                    "name": c.get("name", ""),
                                    "path": c.get("path", "/"),
                                    "value": c.get("value", ""),
                                    "secure": c.get("secure", False),
                                    "expires": int(c.get("expirationDate", 0))
                                    if "expirationDate" in c
                                    else 0,
                                    "http_only": c.get("httpOnly", False),
                                }
                            )
                        cookies_dicts = CookieCleaner.clean(
                            cookies_dicts,
                            platform,
                            config_manager.get("cookie_cleaning_enabled", True),
                        )

                        # 写入 Netscape 格式缓存
                        self._write_netscape_file(cookies_dicts, cache_file)
                        self._mark_current_webview2_account_refreshed(
                            platform=platform,
                            cache_file=cache_file,
                            cookie_count=len(cookies_dicts),
                            valid=True,
                        )

                        log_text(
                            logger,
                            "info",
                            "WebView2 登录成功，Cookie 已保存: {0} ({1} 个)",
                            cache_file,
                            len(cookies_dicts),
                        )

                    except Exception as e:
                        log_text(logger, "error", "WebView2 登录流程失败: {0}", e)
                        self._last_status = AuthStatus(
                            valid=False,
                            message=tr_text("登录失败: {0}", e),
                        )
                        self._mark_current_webview2_account_refreshed(
                            platform=platform,
                            cache_file=cache_file,
                            cookie_count=0,
                            valid=False,
                        )
                        return None

                # 非强制刷新时，仅使用缓存
                if cache_file.exists():
                    self._update_status_from_file(str(cache_file))
                    return str(cache_file)
                else:
                    log_text(
                        logger,
                        "info",
                        "WebView2 模式：无缓存 Cookie，请在设置页点击「立即刷新」登录获取",
                    )
                    self._last_status = AuthStatus(
                        valid=False,
                        message=tr_text("尚未登录获取 Cookie，请在设置页点击「立即刷新」"),
                    )
                    return None

            elif self._current_source in BROWSER_SOURCES:
                # 浏览器来源：使用 rookiepy 提取
                return self._extract_and_cache(
                    browser=self._current_source.value,
                    platform=platform,
                    force_refresh=force_refresh or self._auto_refresh,
                )

        except Exception as e:
            log_text(logger, "error", "获取 Cookie 失败: {0}", e)
            self._last_status = AuthStatus(
                valid=False,
                message=tr_text("获取失败: {0}", e),
            )

        return None

    def refresh_now(self, platform: str = "youtube") -> AuthStatus:
        """
        立即刷新 Cookie

        Returns:
            刷新后的状态
        """
        if self._current_source == AuthSourceType.NONE:
            self._last_status = AuthStatus(valid=False, message=tr_text("未启用验证"))
            return self._last_status

        try:
            cookie_path = self.get_cookie_file_for_ytdlp(platform, force_refresh=True)
            if cookie_path:
                return self._last_status
        except Exception as e:
            self._last_status = AuthStatus(valid=False, message=tr_text("刷新失败: {0}", e))

        return self._last_status

    def validate_file(self, file_path: str, platform: str = "youtube") -> AuthStatus:
        """
        验证 Cookie 文件

        Args:
            file_path: cookies.txt 路径
            platform: 目标平台（youtube / twitter）。必须传对 —— 用 "youtube" 去校验
                X 的文件时，YOUTUBE_ALLOWED_NAMES 白名单会把 auth_token / ct0 全部剥光，
                结果永远是"文件为空或格式无效"。

        Returns:
            验证结果
        """
        try:
            path = Path(file_path)
            if not path.exists():
                return AuthStatus(valid=False, message=tr_text("文件不存在"))

            content = path.read_text(encoding="utf-8", errors="replace")
            cookies = self._parse_netscape_cookies(content)

            # 手动导入文件时，先进行合规清洗以反映实际的有效数量
            from .cookie_cleaner import CookieCleaner

            cookies = CookieCleaner.clean(
                cookies, platform, config_manager.get("cookie_cleaning_enabled", True)
            )

            if not cookies:
                return AuthStatus(valid=False, message=tr_text("文件为空或格式无效"))

            validation = self._validate_cookies(cookies, platform)

            return AuthStatus(
                valid=validation["valid"],
                message=validation["message"],
                cookie_count=len(cookies),
                last_updated=datetime.now().isoformat(),
            )

        except Exception as e:
            return AuthStatus(valid=False, message=tr_text("验证失败: {0}", e))

    def import_manual_cookie_file(self, file_path: str, platform: str = "youtube") -> AuthStatus:
        """
        全量导入并接管 Cookies 文件

        读取 → 清洗 → 写平台缓存 → 过闸门写 bin/cookies_<platform>.txt。

        以前这里无视 platform，一律写 `cookie_sentinel.cookie_path`（永远是
        cookies_youtube.txt）—— 用户导入一份 X 的 Cookie，结果覆盖掉了 YouTube 真相源。
        """
        try:
            path = Path(file_path)
            content = path.read_text(encoding="utf-8", errors="replace")
            cookies = self._parse_netscape_cookies(content)

            # 强化清洗过滤
            from .cookie_cleaner import CookieCleaner

            cookies = CookieCleaner.clean(
                cookies, platform, config_manager.get("cookie_cleaning_enabled", True)
            )

            # 再校验一次核心凭证
            validation = self._validate_cookies(cookies, platform)
            if not validation["valid"]:
                return AuthStatus(valid=False, message=validation["message"])

            # 写入专属缓存
            cache_file = self.cache_dir / f"cached_file_{platform}.txt"
            self._write_netscape_file(cookies, cache_file)

            # 真相源只经闸门写入（弱回退：不过校验就保留旧文件）
            from .cookie_sentinel import cookie_sentinel

            ok, reason = cookie_sentinel._commit_to_truth_source(cache_file, platform, "file")
            if not ok:
                return AuthStatus(valid=False, message=tr_text("导入未生效: {0}", reason))

            # 最后自我更新状态
            self._update_status_from_file(str(cache_file), platform)
            return self._last_status

        except Exception as e:
            log_text(logger, "error", "导入 Cookie 失败: {0}", e)
            return AuthStatus(valid=False, message=tr_text("导入底层异常: {0}", e))

    # ==================== 内部方法 ====================

    def _extract_and_cache(
        self,
        browser: str,
        platform: str,
        force_refresh: bool = False,
    ) -> str | None:
        """
        从浏览器提取 Cookie 并缓存 (rookiepy 方式)

        WebView2 登录获取已由 get_cookie_file_for_ytdlp 中的 WebView2 分支处理，
        此方法仅用于传统浏览器提取 (rookiepy)。
        """

        if not HAS_ROOKIEPY:
            raise RuntimeError(tr_text("rookiepy 未安装，无法从浏览器提取 Cookie"))

        # 缓存文件路径
        cache_file = self.cache_dir / f"cached_{browser}_{platform}.txt"

        # 检查缓存是否足够新（5 分钟内）
        if not force_refresh and cache_file.exists():
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
            age_minutes = (datetime.now() - mtime).total_seconds() / 60
            if age_minutes < 5:
                log_text(logger, "debug", "使用缓存的 Cookie 文件: {0}", cache_file)
                self._update_status_from_file(str(cache_file))
                return str(cache_file)

        # 提取 Cookie
        domains = PLATFORM_DOMAINS.get(platform, [".youtube.com", ".google.com"])
        cookies = None

        try:
            # 原生支持组直接提取
            extractor = getattr(rookiepy, browser, None)
            if extractor is None:
                raise RuntimeError(tr_text("rookiepy 不支持 {0}", browser))
            cookies = extractor(domains)

            log_text(logger, "info", "从 {0} 提取到 {1} 个 Cookie", browser, len(cookies))

            # 使用 CookieCleaner 进行合规清洗
            cookies = CookieCleaner.clean(
                cookies, platform, config_manager.get("cookie_cleaning_enabled", True)
            )

        except Exception as e:
            log_text(logger, "warning", "直接提取失败: {0}", e)

            error_str = str(e).lower()

            # 检测是否为 App-Bound 加密错误（Chrome/Edge v127+ 的解密失败）或权限问题
            is_decryption_failed = "decrypt_encrypted_value failed" in error_str
            is_admin_needed = _is_appbound_error(e) and sys.platform == "win32"

            if is_decryption_failed or is_admin_needed:
                browser_display = self.current_source_display

                # 如果是明确的解密失败，说明即使用管理员也无法绕过当前版本的防护
                if is_decryption_failed:
                    log_text(
                        logger, "info", "检测到 Chromium 严格依赖本地执行的 App-Bound 加密拒绝解密"
                    )

                    self._last_status = AuthStatus(
                        valid=False,
                        message=(
                            tr_text(
                                '【提取解密失败】\n\n受到 {0} 最新的底层加密机制 (App-Bound Encryption) 限制，目前第三方工具无法直接解密提取它的 Cookie。\n\n请执行以下任一替代方案：\n1. 切换到不受此限制的浏览器 (推荐: Firefox 或 LibreWolf)\n2. 使用「手动导入」方式 (前往设置选择"手动导入 cookies.txt"并提供导出的文件)',
                                browser_display,
                            )
                        ),
                    )
                    # 返回 None 意味着失败，不再抛出异常触发管理员提权弹窗
                    return None

                else:
                    # 原本的安全降级，可能只需要管理员权限
                    log_text(logger, "info", "检测到 Chrome v130+ App-Bound 加密，需要管理员权限")
                    self._last_status = AuthStatus(
                        valid=False,
                        message=tr_text(
                            "{0} 需要管理员权限才能提取 Cookie（App-Bound 加密）", browser_display
                        ),
                    )
                    raise PermissionError(
                        tr_text(
                            "{0} v130+ 使用了 App-Bound 加密。\n需要以管理员身份重新启动程序才能提取 Cookie。\n\n建议：使用 Edge 或 Firefox 浏览器可避免此问题。",
                            browser_display,
                        )
                    ) from e
            else:
                # 非 App-Bound / 解密相关的其他错误，直接抛出
                raise

        if not cookies:
            browser_display = self.current_source_display
            self._last_status = AuthStatus(
                valid=False,
                message=(
                    tr_text(
                        "无法从 {0} 提取 Cookie\n\n可能的原因：\n1. {1} 未安装\n2. 未在 {2} 中登录 YouTube\n3. {3} 正在运行（Cookie 数据库被锁定）\n\n建议：\n• 确保已在浏览器中登录 YouTube\n• 完全关闭浏览器后重试\n• 尝试使用其他浏览器（如 Edge）",
                        browser_display,
                        browser_display,
                        browser_display,
                        browser_display,
                    )
                ),
            )
            return None

        # 写入缓存文件
        self._write_netscape_file(cookies, cache_file)

        # 验证并更新状态
        validation = self._validate_cookies(cookies, platform)

        self._last_status = AuthStatus(
            valid=validation["valid"],
            message=validation["message"],
            cookie_count=len(cookies),
            last_updated=datetime.now().isoformat(),
            account_hint=self._detect_account_hint(cookies),
        )

        return str(cache_file)

    def _write_netscape_file(self, cookies: list[dict], output_path: Path) -> None:
        """将 Cookie 写入 Netscape 格式文件"""
        lines = [
            "# Netscape HTTP Cookie File",
            "# Generated by FluentYTDL AuthService",
            f"# {datetime.now().isoformat()}",
            "",
        ]

        for c in cookies:
            domain = c.get("domain", "")
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path", "/")
            secure = "TRUE" if c.get("secure", False) else "FALSE"
            expiry = str(int(c.get("expires", 0) or 0))
            name = c.get("name", "")
            value = c.get("value", "")

            lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")

        output_path.write_text("\n".join(lines), encoding="utf-8")
        log_text(logger, "debug", "已生成 Cookie 文件: {0}", output_path)

    def _parse_netscape_cookies(self, content: str) -> list[dict]:
        """解析 Netscape 格式的 Cookie 文件"""
        cookies = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) >= 7:
                cookies.append(
                    {
                        "domain": parts[0],
                        "path": parts[2],
                        "secure": parts[3].upper() == "TRUE",
                        "expires": int(parts[4]) if parts[4].isdigit() else 0,
                        "name": parts[5],
                        "value": parts[6],
                    }
                )
        return cookies

    def _validate_cookies(self, cookies: list[dict], platform: str) -> dict:
        """验证 Cookie 时间戳有效性与核心组件遗漏情况"""
        from datetime import datetime

        current_time = int(datetime.now().timestamp())

        valid_cookies = []
        for c in cookies:
            expires = int(c.get("expires", 0) or 0)
            # expires == 0 represents a session cookie; naturally assumed to be valid
            if expires == 0 or expires > current_time:
                valid_cookies.append(c)

        found = {c.get("name", "") for c in valid_cookies}

        if platform == "youtube":
            required = YOUTUBE_REQUIRED_COOKIES
            missing = required - found

            if missing:
                return {
                    "valid": False,
                    "message": tr_text("Cookie 不完整，缺少: {0}", ", ".join(missing)),
                }

            # 提交闸门（见 `YOUTUBE_AUTH_MARKERS`）：上面的 name-only 检查会被「只剩
            # .google.com SID 家族」的半 jar 骗过——那正是 yt-dlp 回写登出态后的典型残骸。
            # 这里再要求至少一个认证态 marker 真的落在 `.youtube.com` 域上（域判定复用
            # `CookieCleaner._domain_allowed`：相等或子域，已处理前缀点）。仅限 youtube 分支
            # ——twitter/通用分支与其它平台都不受影响，也不给任何 Cookie 强加全局
            # `.youtube.com` 要求。
            from .cookie_cleaner import CookieCleaner

            has_youtube_auth = any(
                c.get("name", "") in YOUTUBE_AUTH_MARKERS
                and CookieCleaner._domain_allowed(c.get("domain", ""), {".youtube.com"})
                for c in valid_cookies
            )
            if not has_youtube_auth:
                return {
                    "valid": False,
                    "message": (
                        tr_text(
                            "缺少 .youtube.com 登录态 Cookie（LOGIN_INFO / SID / SAPISID / __Secure-1PSID 之一）"
                        )
                    ),
                }

            return {
                "valid": True,
                "message": tr_text("已验证 (检测到 YouTube 登录)"),
            }
        elif platform == "twitter":
            required = X_REQUIRED_COOKIES
            missing = required - found

            if missing:
                return {
                    "valid": False,
                    "message": tr_text(
                        "X 平台 Cookie 不完整，缺少关键字段: {0}", ", ".join(missing)
                    ),
                }
            return {
                "valid": True,
                "message": tr_text("已验证 (检测到 X 平台登录)"),
            }
        else:
            if valid_cookies:
                return {
                    "valid": True,
                    "message": tr_text("找到 {0} 个有效 Cookie", len(valid_cookies)),
                }
            return {"valid": False, "message": tr_text("未找到有效 Cookie")}

    def _detect_account_hint(self, cookies: list[dict]) -> str | None:
        """尝试检测账户信息"""
        # 检查是否有 Premium 相关标识
        for c in cookies:
            name = c.get("name", "").lower()
            value = c.get("value", "").lower()
            if "premium" in name or "premium" in value:
                return "YouTube Premium"
        return None

    def _update_status_from_file(self, file_path: str, platform: str = "youtube") -> None:
        """从文件更新状态"""
        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="replace")
            cookies = self._parse_netscape_cookies(content)
            validation = self._validate_cookies(cookies, platform)

            self._last_status = AuthStatus(
                valid=validation["valid"],
                message=validation["message"],
                cookie_count=len(cookies),
                last_updated=datetime.fromtimestamp(Path(file_path).stat().st_mtime).isoformat(),
            )
        except Exception as e:
            self._last_status = AuthStatus(valid=False, message=tr_text("读取失败: {0}", e))

    # ==================== 配置持久化 ====================

    def _save_config(self) -> None:
        """保存配置"""
        data = {
            "version": 3,
            "source": self._current_source.value,
            "file_path": self._current_file_path,
            "auto_refresh": self._auto_refresh,
            "current_webview2_account_ids": self._current_webview2_account_ids,
            "updated_at": datetime.now().isoformat(),
        }
        with open(self._config_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))

    def _load_config(self) -> None:
        """加载配置"""
        if not self._config_path.exists():
            # 默认使用 WebView2 登录获取
            self._current_source = AuthSourceType.WEBVIEW2
            self._auto_refresh = False
            log_text(logger, "info", "首次启动，默认使用 WebView2 登录获取验证")
            self._save_config()
            return

        try:
            with open(self._config_path, encoding="utf-8") as f:
                data = json.load(f)
            source_value = data.get("source", "webview2")  # 默认 dle
            # 兼容旧配置
            if source_value == "none" or source_value == "dle":
                source_value = "webview2"
            self._current_source = AuthSourceType(source_value)
            self._current_file_path = data.get("file_path")
            self._auto_refresh = data.get("auto_refresh", True)
            if "current_webview2_account_ids" in data:
                self._current_webview2_account_ids = data["current_webview2_account_ids"]
            elif "current_webview2_account_id" in data:
                # 兼容旧配置
                legacy_id = data["current_webview2_account_id"]
                if legacy_id:
                    self._current_webview2_account_ids = {"youtube": legacy_id}
                else:
                    self._current_webview2_account_ids = {}

            # 已停止支持的提取源一次性迁到 Edge 并写回 —— 只迁一次。
            # 不能靠 UI 兜底：下拉框选不中 chrome 时会停在索引 0，配置里却还是 chrome，
            # 下次启动照旧走 Chrome 分支提取失败。
            migrated_from = None
            if self._current_source in LEGACY_SOURCES:
                migrated_from = self._current_source.value
                self._current_source = AuthSourceType.EDGE

            log_text(logger, "info", "已加载验证配置: {0}", self.current_source_display)

            if migrated_from:
                log_text(logger, "info", "提取源 {0} 已停止支持，已自动迁移到 Edge", migrated_from)
                self._save_config()

            # 尝试恢复上次的验证状态
            self._restore_last_status()
        except Exception as e:
            log_text(logger, "error", "加载验证配置失败: {0}", e)
            # 加载失败时使用默认的 DLE
            self._current_source = AuthSourceType.WEBVIEW2

    def _get_webview2_cache_file(self, platform: str = "youtube") -> Path:
        """获取当前 WebView2 账号对应的缓存文件路径"""
        self._ensure_current_webview2_account_valid(platform)
        account = self.get_current_webview2_account(platform)
        if account and account.cached_cookie_path:
            return Path(account.cached_cookie_path)

        # 兜底：兼容老路径
        return self.cache_dir / f"cached_webview2_{platform}.txt"

    def _build_webview2_account_paths(
        self, account_id: str, platform: str = "youtube"
    ) -> tuple[Path, Path]:
        """构建 WebView2 账号 profile 与缓存路径"""
        root = self._webview2_accounts_dir / platform / account_id
        profile_dir = root / "profile"
        cache_file = root / "cookies.txt"
        profile_dir.mkdir(parents=True, exist_ok=True)
        return profile_dir, cache_file

    def _mark_current_webview2_account_refreshed(
        self,
        platform: str,
        cache_file: Path,
        cookie_count: int,
        valid: bool,
    ) -> None:
        """刷新后回写当前 WebView2 账号状态"""
        account = self.get_current_webview2_account(platform)
        if not account:
            return

        account.cached_cookie_path = str(cache_file)
        account.last_extracted_at = datetime.now().isoformat()
        account.cookie_count = cookie_count
        account.valid = valid
        self._save_webview2_accounts()

    # ==================== WebView2 多账号管理 ====================

    def list_webview2_accounts(self, platform: str | None = None) -> list[WebView2Account]:
        """列出 WebView2 账号"""
        self._load_webview2_accounts()
        if platform:
            return [a for a in self._webview2_accounts.values() if a.platform == platform]
        return list(self._webview2_accounts.values())

    def create_webview2_account(
        self,
        display_name: str,
        platform: str = "youtube",
        notes: str | None = None,
        *,
        builtin_name: str = "",
    ) -> WebView2Account:
        """创建 WebView2 账号"""
        display_name = display_name.strip()
        if not display_name:
            display_name = "Unnamed Account"
            builtin_name = "unnamed"

        # 检查是否重复命名（全局范围检查，防止跨平台混淆）
        for existing in self._webview2_accounts.values():
            if existing.display_name == display_name:
                raise ValueError(tr_text("已存在名为 '{0}' 的账号，请使用其他名称", display_name))

        account_id = uuid4().hex
        profile_dir, cache_file = self._build_webview2_account_paths(account_id, platform)
        account = WebView2Account(
            account_id=account_id,
            display_name=display_name,
            builtin_name=builtin_name,
            platform=platform,
            profile_dir=str(profile_dir),
            cached_cookie_path=str(cache_file),
            is_default=(
                sum(1 for a in self._webview2_accounts.values() if a.platform == platform) == 0
            ),
            notes=notes,
        )
        self._webview2_accounts[account_id] = account

        if platform not in self._current_webview2_account_ids:
            self._current_webview2_account_ids[platform] = account_id
            self._save_config()

        self._save_webview2_accounts()
        return account

    def update_webview2_account(
        self,
        account_id: str,
        *,
        display_name: str | None = None,
        notes: str | None = None,
        is_default: bool | None = None,
    ) -> bool:
        """更新 WebView2 账号元信息"""
        account = self._webview2_accounts.get(account_id)
        if not account:
            return False

        if display_name is not None:
            new_name = display_name.strip() or account.display_name
            if new_name != account.display_name:
                for existing in self._webview2_accounts.values():
                    if existing.display_name == new_name and existing.account_id != account_id:
                        raise ValueError(
                            tr_text("已存在名为 '{0}' 的账号，请使用其他名称", new_name)
                        )
            account.display_name = new_name
            if display_name.strip():
                account.builtin_name = ""
        if notes is not None:
            account.notes = notes
        if is_default is True:
            for a in self._webview2_accounts.values():
                if a.platform == account.platform:
                    a.is_default = False
            account.is_default = True

        self._save_webview2_accounts()
        return True

    def delete_webview2_account(self, account_id: str, remove_storage: bool = False) -> bool:
        """删除 WebView2 账号"""
        account = self._webview2_accounts.get(account_id)
        if not account:
            return False

        same_platform_count = sum(
            1 for a in self._webview2_accounts.values() if a.platform == account.platform
        )
        if same_platform_count <= 1:
            log_text(
                logger, "warning", "至少需要保留一个 {0} WebView2 账号，拒绝删除", account.platform
            )
            return False

        self._webview2_accounts.pop(account_id, None)

        if remove_storage:
            account_root = self._webview2_accounts_dir / account.platform / account_id
            try:
                shutil.rmtree(account_root, ignore_errors=True)
            except Exception as e:
                log_text(logger, "warning", "删除 WebView2 账号存储目录失败: {0}", e)

        if account_id == self._current_webview2_account_ids.get(account.platform):
            self._current_webview2_account_ids[account.platform] = next(
                (
                    a.account_id
                    for a in self._webview2_accounts.values()
                    if a.platform == account.platform
                ),
                None,
            )
            self._save_config()

        # 保证始终有一个同平台的默认账号
        if not any(
            a.is_default for a in self._webview2_accounts.values() if a.platform == account.platform
        ):
            first = next(
                (a for a in self._webview2_accounts.values() if a.platform == account.platform),
                None,
            )
            if first:
                first.is_default = True

        self._save_webview2_accounts()
        return True

    def set_current_webview2_account(self, account_id: str) -> bool:
        """设置当前激活 WebView2 账号"""
        account = self._webview2_accounts.get(account_id)
        if not account:
            return False
        self._current_webview2_account_ids[account.platform] = account_id
        self._save_config()

        # 按用户预期：切换 WebView2 账号时，立即将该账号 Cookie 同步到对应的 bin/cookies_xxx.txt
        self._sync_current_webview2_cookie_to_unified_cookiefile(platform=account.platform)
        return True

    def _sync_current_webview2_cookie_to_unified_cookiefile(
        self, platform: str = "youtube"
    ) -> bool:
        """将当前 WebView2 账号的 Cookie 覆盖同步到对应的统一 Cookie 文件"""
        account = self.get_current_webview2_account(platform)
        if not account or not account.cached_cookie_path:
            return False

        src = Path(account.cached_cookie_path)
        if not src.exists():
            log_text(
                logger, "info", "当前 WebView2 账号 ({0}) 尚无 Cookie 缓存，跳过同步", platform
            )
            return False

        try:
            from fluentytdl.auth.cookie_sentinel import cookie_sentinel

            # 经过真相源写入闸门：新账号 Cookie 不可用时不强制替换（弱回退）
            ok, reason = cookie_sentinel._commit_to_truth_source(
                src, platform, f"webview2:{account.account_id}"
            )
            if ok:
                log_text(
                    logger,
                    "info",
                    "已切换到 WebView2 账号 {0}，并同步 Cookie 到 {1}",
                    account.localized_name,
                    cookie_sentinel.get_cookie_path_for_platform(platform),
                )
            else:
                log_text(
                    logger,
                    "warning",
                    "WebView2 账号 {0} 的 Cookie 未通过校验，已保留原有 {1} 真相源: {2}",
                    account.localized_name,
                    platform,
                    reason,
                )
            return ok
        except Exception as e:
            log_text(logger, "warning", "同步当前 WebView2 账号 Cookie 到统一文件失败: {0}", e)
            return False

    def _save_webview2_accounts(self) -> None:
        """保存 WebView2 账号配置"""
        data = {
            "version": 1,
            "accounts": [a.to_dict() for a in self._webview2_accounts.values()],
            "updated_at": datetime.now().isoformat(),
        }
        with open(self._webview2_accounts_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))

    def _load_webview2_accounts(self) -> None:
        """加载 WebView2 账号配置"""
        self._webview2_accounts = {}
        old_accounts_path = self.cache_dir / "webview2_accounts.json"
        path_to_load = (
            self._webview2_accounts_path
            if self._webview2_accounts_path.exists()
            else old_accounts_path
        )
        if not path_to_load.exists():
            return
        try:
            with open(path_to_load, encoding="utf-8") as f:
                data = json.load(f)

            for raw in data.get("accounts", []):
                acc = WebView2Account.from_dict(raw)
                if not acc.account_id:
                    continue
                # 将账号目录统一迁移到 bin/dle_user/<account_id>/ 下
                profile_dir, cache_file = self._build_webview2_account_paths(
                    acc.account_id, acc.platform
                )
                old_cookie = Path(acc.cached_cookie_path) if acc.cached_cookie_path else None

                # 迁移旧 cookie 文件到新位置（若新位置尚不存在）
                try:
                    if (
                        old_cookie
                        and old_cookie.exists()
                        and old_cookie != cache_file
                        and not cache_file.exists()
                    ):
                        cache_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(old_cookie, cache_file)
                except Exception as e:
                    log_text(
                        logger, "warning", "迁移账号 {0} Cookie 文件失败: {1}", acc.account_id, e
                    )

                # 路径统一写回新结构
                acc.profile_dir = str(profile_dir)
                acc.cached_cookie_path = str(cache_file)
                self._webview2_accounts[acc.account_id] = acc

            # 若是从旧位置加载，落盘到新位置
            if path_to_load != self._webview2_accounts_path:
                self._save_webview2_accounts()
        except Exception as e:
            log_text(logger, "error", "加载 WebView2 账号配置失败: {0}", e)

    def _ensure_current_webview2_account_valid(self, platform: str = "youtube") -> None:
        """确保当前激活 WebView2 账号存在"""
        accounts = [a for a in self._webview2_accounts.values() if a.platform == platform]
        # 没有任何账号时创建默认账号
        if not accounts:
            platform_name = (
                "YouTube" if platform == "youtube" else "X" if platform == "x" else platform
            )
            default = self.create_webview2_account(
                f"{platform_name} Default Account",
                platform=platform,
                builtin_name="platform_default",
            )
            default.is_default = True
            accounts = [default]
            self._save_webview2_accounts()

        if self._current_webview2_account_ids.get(platform) in self._webview2_accounts:
            return

        # 优先默认账号，其次第一个
        default = next((a for a in accounts if a.is_default), None)
        chosen = default or accounts[0]
        if chosen:
            self._current_webview2_account_ids[platform] = chosen.account_id
            self._save_config()

    def _migrate_legacy_webview2_cache_if_needed(self) -> None:
        """将旧单账号 WebView2 缓存迁移到默认账号"""
        legacy = self.cache_dir / "cached_webview2_youtube.txt"
        if not legacy.exists():
            return

        # 已经迁移过（存在账号化缓存）则不再处理
        has_account_cache = any(
            Path(a.cached_cookie_path).exists()
            for a in self._webview2_accounts.values()
            if a.cached_cookie_path
        )
        if has_account_cache:
            return

        self._ensure_current_webview2_account_valid("youtube")
        account = self.get_current_webview2_account("youtube")
        if not account:
            return

        try:
            target = Path(account.cached_cookie_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, target)
            account.last_extracted_at = datetime.now().isoformat()
            account.valid = True
            self._save_webview2_accounts()
            log_text(
                logger,
                "info",
                "已将旧 WebView2 缓存迁移到账号 {0}: {1}",
                account.localized_name,
                target,
            )

            # 若当前正在 WebView2 模式，迁移后同步到统一 cookiefile
            if self._current_source == AuthSourceType.WEBVIEW2:
                self._sync_current_webview2_cookie_to_unified_cookiefile()
        except Exception as e:
            log_text(logger, "warning", "迁移旧 WebView2 缓存失败: {0}", e)

    def _migrate_accounts_to_platform_isolated_dirs(self) -> None:
        """将无平台的 `dle_user/<account_id>` 旧目录物理迁移到 `dle_user/<platform>/<account_id>` 下"""
        changed = False
        for account in self._webview2_accounts.values():
            old_root = self._webview2_accounts_dir / account.account_id
            new_root = self._webview2_accounts_dir / account.platform / account.account_id

            # 如果旧目录存在，并且新目录不存在，执行移动
            if old_root.exists() and not new_root.exists():
                try:
                    new_root.parent.mkdir(parents=True, exist_ok=True)
                    old_root.rename(new_root)
                    # 更新 account 中的路径
                    account.profile_dir = str(new_root / "profile")
                    account.cached_cookie_path = str(new_root / "cookies.txt")
                    changed = True
                    log_text(logger, "info", "已将账号目录隔离至新路径: {0}", new_root)
                except Exception as e:
                    log_text(logger, "warning", "迁移账号目录失败 {0}: {1}", account.account_id, e)

        if changed:
            self._save_webview2_accounts()

    def _resolve_runtime_bin_dir(self) -> Path:
        """解析运行目录下的 bin 路径（开发态/打包态统一）。"""
        try:
            from ..utils.paths import frozen_app_dir, is_frozen, project_root

            root = frozen_app_dir() if is_frozen() else project_root()
            p = root / "bin"
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            fallback = self.cache_dir / "bin"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def _restore_last_status(self) -> None:
        """恢复上次的验证状态（从缓存文件）"""
        if self._current_source == AuthSourceType.NONE:
            return

        try:
            if self._current_source == AuthSourceType.FILE:
                # 文件模式：检查文件是否存在
                if self._current_file_path and Path(self._current_file_path).exists():
                    self._update_status_from_file(self._current_file_path)
            elif self._current_source in BROWSER_SOURCES:
                # 浏览器模式：检查缓存文件
                cache_file = self.cache_dir / f"cached_{self._current_source.value}_youtube.txt"
                if cache_file.exists():
                    self._update_status_from_file(str(cache_file))
                    # 检查缓存是否过期（超过 1 小时标记为需要刷新）
                    mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
                    age_hours = (datetime.now() - mtime).total_seconds() / 3600
                    if age_hours > 1:
                        self._last_status.message += tr_text(" (缓存可能过期)")
        except Exception as e:
            log_text(logger, "debug", "恢复状态失败: {0}", e)

    # ==================== 高级：多账户管理 ====================

    def get_profiles(self) -> list[AuthProfile]:
        """获取所有配置文件"""
        self._load_profiles()
        return list(self._profiles.values())

    def add_profile(self, profile: AuthProfile) -> None:
        """添加配置"""
        key = f"{profile.platform}_{profile.name}"
        self._profiles[key] = profile
        self._save_profiles()

    def remove_profile(self, name: str, platform: str = "youtube") -> bool:
        """移除配置"""
        key = f"{platform}_{name}"
        if key in self._profiles:
            del self._profiles[key]
            self._save_profiles()
            return True
        return False

    def _save_profiles(self) -> None:
        """保存配置文件"""
        data = {
            "version": 1,
            "profiles": [p.to_dict() for p in self._profiles.values()],
        }
        with open(self._profiles_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))

    def _load_profiles(self) -> None:
        """加载配置文件"""
        if not self._profiles_path.exists():
            return
        try:
            with open(self._profiles_path, encoding="utf-8") as f:
                data = json.load(f)
            for p_data in data.get("profiles", []):
                profile = AuthProfile.from_dict(p_data)
                key = f"{profile.platform}_{profile.name}"
                self._profiles[key] = profile
        except Exception as e:
            log_text(logger, "error", "加载配置文件失败: {0}", e)

    def cleanup_cache(self, max_age_hours: int = 24) -> int:
        """清理过期缓存"""
        cleaned = 0
        now = datetime.now()

        for f in self.cache_dir.glob("cached_*.txt"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                age_hours = (now - mtime).total_seconds() / 3600
                if age_hours > max_age_hours:
                    f.unlink()
                    cleaned += 1
            except Exception:
                pass

        return cleaned


# 全局单例
auth_service = AuthService()
