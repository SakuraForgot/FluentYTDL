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
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..utils.logger import logger
from .cookie_cleaner import CookieCleaner

# 尝试导入 rookiepy
try:
    import rookiepy

    HAS_ROOKIEPY = True
except ImportError:
    rookiepy = None
    HAS_ROOKIEPY = False
    logger.warning("rookiepy 未安装，浏览器 Cookie 自动提取功能不可用")


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
    CHROME = "chrome"  # Google Chrome
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
    CENT = "centbrowser"  # 百分浏览器
    # DLE 登录获取
    DLE = "dle"  # 动态本地插件注入登录
    # 其他
    FILE = "file"  # 手动导入的 cookies.txt


# 浏览器类型列表（用于 UI 展示和逻辑判断）
# Chromium 内核 v130+ 都需要管理员权限提取 Cookie
BROWSER_SOURCES = [
    AuthSourceType.EDGE,
    AuthSourceType.CHROME,
    AuthSourceType.CHROMIUM,
    AuthSourceType.BRAVE,
    AuthSourceType.OPERA,
    AuthSourceType.OPERA_GX,
    AuthSourceType.VIVALDI,
    AuthSourceType.ARC,
    AuthSourceType.FIREFOX,
    AuthSourceType.LIBREWOLF,
    AuthSourceType.CENT,
]

# 需要管理员权限的浏览器（Chromium 内核 v130+）
ADMIN_REQUIRED_BROWSERS = [
    AuthSourceType.EDGE,
    AuthSourceType.CHROME,
    AuthSourceType.CHROMIUM,
    AuthSourceType.BRAVE,
    AuthSourceType.OPERA,
    AuthSourceType.OPERA_GX,
    AuthSourceType.VIVALDI,
    AuthSourceType.ARC,
    AuthSourceType.CENT,
]

# 各平台需要的 Cookie 域名
PLATFORM_DOMAINS = {
    "youtube": [".youtube.com", ".google.com"],
    "bilibili": [".bilibili.com"],
    "twitter": [".twitter.com", ".x.com"],
}

# YouTube 登录验证所需的关键 Cookie
YOUTUBE_REQUIRED_COOKIES = {"SID", "HSID", "SSID", "SAPISID", "APISID"}


@dataclass
class AuthStatus:
    """验证状态"""

    valid: bool = False
    message: str = "未验证"
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
class DLEAccount:
    """DLE 多账号配置"""

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DLEAccount:
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
        self._dle_accounts_dir = self._runtime_bin_dir / "dle_user"
        self._dle_accounts_path = self._dle_accounts_dir / "accounts.json"
        self._dle_accounts_dir.mkdir(parents=True, exist_ok=True)

        # 当前配置
        self._current_source: AuthSourceType = AuthSourceType.NONE
        self._current_file_path: str | None = None
        self._auto_refresh: bool = True
        self._last_status: AuthStatus = AuthStatus()
        self._current_dle_account_id: str | None = None

        # 高级：多账户配置
        self._profiles: dict[str, AuthProfile] = {}
        self._dle_accounts: dict[str, DLEAccount] = {}

        self._load_config()
        self._load_dle_accounts()
        self._migrate_legacy_dle_cache_if_needed()
        self._ensure_current_dle_account_valid()

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
            AuthSourceType.NONE: "未启用",
            AuthSourceType.EDGE: "Edge 浏览器",
            AuthSourceType.CHROME: "Chrome 浏览器",
            AuthSourceType.CHROMIUM: "Chromium 浏览器",
            AuthSourceType.BRAVE: "Brave 浏览器",
            AuthSourceType.OPERA: "Opera 浏览器",
            AuthSourceType.OPERA_GX: "Opera GX 浏览器",
            AuthSourceType.VIVALDI: "Vivaldi 浏览器",
            AuthSourceType.ARC: "Arc 浏览器",
            AuthSourceType.FIREFOX: "Firefox 浏览器",
            AuthSourceType.LIBREWOLF: "LibreWolf 浏览器",
            AuthSourceType.CENT: "百分浏览器 (Cent)",
            AuthSourceType.DLE: "登录获取 (推荐)",
            AuthSourceType.FILE: "手动导入文件",
        }
        return names.get(self._current_source, "未知")

    @property
    def auto_refresh(self) -> bool:
        """是否自动刷新 Cookie"""
        return self._auto_refresh

    @property
    def last_status(self) -> AuthStatus:
        """最近一次验证状态"""
        return self._last_status

    @property
    def current_dle_account_id(self) -> str | None:
        """当前激活的 DLE 账号 ID"""
        return self._current_dle_account_id

    @property
    def current_dle_account(self) -> DLEAccount | None:
        """当前激活的 DLE 账号"""
        if not self._current_dle_account_id:
            return None
        return self._dle_accounts.get(self._current_dle_account_id)

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

        logger.info(f"验证源已设置: {self.current_source_display}")

        # 注意：不再立即清理旧 Cookie 文件
        # 延迟到实际提取成功后再清理，避免提取失败时丢失旧 Cookie
        if old_source != source:
            logger.info(f"验证源变化: {old_source.value} -> {source.value}")
            logger.info("将在下次成功提取后更新 Cookie 文件")

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

                        # 清洗
                        cookies = CookieCleaner.clean(cookies, platform)
                        cleaned_count = len(cookies)
                        if original_count != cleaned_count:
                            logger.info(
                                f"[{platform}] 导入文件 Cookie 清洗完成: {original_count} -> {cleaned_count} (移除: {original_count - cleaned_count})"
                            )
                        else:
                            logger.info(
                                f"[{platform}] 导入文件 Cookie 清洗完成: 保持 {original_count} 个"
                            )

                        # 写入特殊的缓存文件
                        self._write_netscape_file(cookies, cache_file)

                    self._update_status_from_file(str(cache_file))
                    return str(cache_file)
                else:
                    self._last_status = AuthStatus(
                        valid=False,
                        message="Cookie 文件不存在",
                    )
                    return None

            elif self._current_source == AuthSourceType.DLE:
                # DLE 登录获取：使用动态插件提取
                cache_file = self._get_dle_cache_file(platform)

                # DLE 是交互式流程，仅在用户显式点击刷新 (force_refresh=True) 时才启动浏览器
                # 其他场景（启动同步、下载前检查）只使用已有缓存
                if force_refresh:
                    try:
                        from .providers.webview2_provider import WebView2CookieProvider

                        account = self.current_dle_account
                        profile_dir = account.profile_dir if account else None
                        account_label = account.display_name if account else "default"
                        profile_has_data = False
                        if profile_dir:
                            try:
                                profile_has_data = Path(profile_dir).exists() and any(
                                    Path(profile_dir).iterdir()
                                )
                            except Exception:
                                profile_has_data = False

                        logger.info(f"开始 DLE 登录流程（账号: {account_label}）...")
                        provider = WebView2CookieProvider()
                        cookies = provider.extract_cookies(
                            storage_path=profile_dir,
                            session_tag=account_label,
                            start_hidden=profile_has_data,
                            reveal_after_seconds=8,
                        )

                        if cookies is None:
                            raise RuntimeError(
                                "WebView2 登录流程超时或返回空数据（详情请见上方日志或检查是否有模块级错误防止了数据回调）"
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
                        cookies_dicts = CookieCleaner.clean(cookies_dicts, platform)

                        # 写入 Netscape 格式缓存
                        self._write_netscape_file(cookies_dicts, cache_file)
                        self._mark_current_dle_account_refreshed(
                            cache_file=cache_file,
                            cookie_count=len(cookies_dicts),
                            valid=True,
                        )

                        logger.info(
                            f"DLE 登录成功，Cookie 已保存: {cache_file} ({len(cookies_dicts)} 个)"
                        )

                    except Exception as e:
                        logger.error(f"DLE 登录流程失败: {e}")
                        self._last_status = AuthStatus(
                            valid=False,
                            message=f"登录失败: {e}",
                        )
                        self._mark_current_dle_account_refreshed(
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
                    logger.info("DLE 模式：无缓存 Cookie，请在设置页点击「立即刷新」登录获取")
                    self._last_status = AuthStatus(
                        valid=False,
                        message="尚未登录获取 Cookie，请在设置页点击「立即刷新」",
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
            logger.error(f"获取 Cookie 失败: {e}")
            self._last_status = AuthStatus(
                valid=False,
                message=f"获取失败: {e}",
            )

        return None

    def refresh_now(self, platform: str = "youtube") -> AuthStatus:
        """
        立即刷新 Cookie

        Returns:
            刷新后的状态
        """
        if self._current_source == AuthSourceType.NONE:
            self._last_status = AuthStatus(valid=False, message="未启用验证")
            return self._last_status

        try:
            cookie_path = self.get_cookie_file_for_ytdlp(platform, force_refresh=True)
            if cookie_path:
                return self._last_status
        except Exception as e:
            self._last_status = AuthStatus(valid=False, message=f"刷新失败: {e}")

        return self._last_status

    def validate_file(self, file_path: str) -> AuthStatus:
        """
        验证 Cookie 文件

        Args:
            file_path: cookies.txt 路径

        Returns:
            验证结果
        """
        try:
            path = Path(file_path)
            if not path.exists():
                return AuthStatus(valid=False, message="文件不存在")

            content = path.read_text(encoding="utf-8", errors="replace")
            cookies = self._parse_netscape_cookies(content)

            # 手动导入文件时，先进行合规清洗以反映实际的有效数量
            from .cookie_cleaner import CookieCleaner

            cookies = CookieCleaner.clean(cookies, "youtube")

            if not cookies:
                return AuthStatus(valid=False, message="文件为空或格式无效")

            validation = self._validate_cookies(cookies, "youtube")

            return AuthStatus(
                valid=validation["valid"],
                message=validation["message"],
                cookie_count=len(cookies),
                last_updated=datetime.now().isoformat(),
            )

        except Exception as e:
            return AuthStatus(valid=False, message=f"验证失败: {e}")

    def import_manual_cookie_file(self, file_path: str, platform: str = "youtube") -> AuthStatus:
        """
        全量导入并接管 Cookies 文件
        读取 -> 格式化 -> 清洗 -> 覆盖 bin/cookies.txt 与缓存
        """
        try:
            path = Path(file_path)
            content = path.read_text(encoding="utf-8", errors="replace")
            cookies = self._parse_netscape_cookies(content)

            # 强化清洗过滤
            from .cookie_cleaner import CookieCleaner

            cookies = CookieCleaner.clean(cookies, platform)

            # 再校验一次核心凭证
            validation = self._validate_cookies(cookies, platform)
            if not validation["valid"]:
                return AuthStatus(valid=False, message=validation["message"])

            # 写入专属缓存
            cache_file = self.cache_dir / f"cached_file_{platform}.txt"
            self._write_netscape_file(cookies, cache_file)

            # 通知 Sentinel 一步到位更新 bin/cookies.txt
            from .cookie_sentinel import cookie_sentinel

            cookie_sentinel.cookie_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_netscape_file(cookies, cookie_sentinel.cookie_path)
            cookie_sentinel._save_meta("file", len(cookies))

            # 最后自我更新状态
            self._update_status_from_file(str(cache_file))
            return self._last_status

        except Exception as e:
            logger.error(f"导入 Cookie 失败: {e}")
            return AuthStatus(valid=False, message=f"导入底层异常: {e}")

    # ==================== 内部方法 ====================

    def _extract_and_cache(
        self,
        browser: str,
        platform: str,
        force_refresh: bool = False,
    ) -> str | None:
        """
        从浏览器提取 Cookie 并缓存 (rookiepy 方式)

        DLE 登录获取已由 get_cookie_file_for_ytdlp 中的 DLE 分支处理，
        此方法仅用于传统浏览器提取 (rookiepy)。
        """

        if not HAS_ROOKIEPY:
            raise RuntimeError("rookiepy 未安装，无法从浏览器提取 Cookie")

        # 缓存文件路径
        cache_file = self.cache_dir / f"cached_{browser}_{platform}.txt"

        # 检查缓存是否足够新（5 分钟内）
        if not force_refresh and cache_file.exists():
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
            age_minutes = (datetime.now() - mtime).total_seconds() / 60
            if age_minutes < 5:
                logger.debug(f"使用缓存的 Cookie 文件: {cache_file}")
                self._update_status_from_file(str(cache_file))
                return str(cache_file)

        # 提取 Cookie
        domains = PLATFORM_DOMAINS.get(platform, [".youtube.com", ".google.com"])
        cookies = None

        try:
            if browser == "centbrowser":
                import os

                # 百分浏览器的默认配置路径
                local_appdata = os.environ.get("LOCALAPPDATA", "")
                cent_user_data_dir = Path(local_appdata) / "CentBrowser" / "User Data"

                if not cent_user_data_dir.exists():
                    raise FileNotFoundError(f"未找到百分浏览器数据目录: {cent_user_data_dir}")

                key_path = str(cent_user_data_dir / "Local State")
                db_path_network = cent_user_data_dir / "Default" / "Network" / "Cookies"
                db_path_legacy = cent_user_data_dir / "Default" / "Cookies"

                if db_path_network.exists():
                    db_path = str(db_path_network)
                elif db_path_legacy.exists():
                    db_path = str(db_path_legacy)
                else:
                    raise FileNotFoundError("未找到百分浏览器的 Cookie 数据库文件。")

                logger.info(f"使用自建提取器提取 CentBrowser Cookie: {db_path}")
                cookies = self._extract_centbrowser_manual(key_path, db_path, domains)
            else:
                # 原生支持组直接提取
                extractor = getattr(rookiepy, browser, None)
                if extractor is None:
                    raise RuntimeError(f"rookiepy 不支持 {browser}")
                cookies = extractor(domains)

            logger.info(f"从 {browser} 提取到 {len(cookies)} 个 Cookie")

            # 使用 CookieCleaner 进行合规清洗
            cookies = CookieCleaner.clean(cookies, platform)

        except Exception as e:
            logger.warning(f"直接提取失败: {e}")

            error_str = str(e).lower()

            # 检测是否为 App-Bound 加密错误（Chrome/Edge v127+ 的解密失败）或权限问题
            is_decryption_failed = "decrypt_encrypted_value failed" in error_str
            is_admin_needed = _is_appbound_error(e) and sys.platform == "win32"

            if is_decryption_failed or is_admin_needed:
                browser_display = self.current_source_display

                # 如果是明确的解密失败，说明即使用管理员也无法绕过当前版本的防护
                if is_decryption_failed:
                    logger.info("检测到 Chromium 严格依赖本地执行的 App-Bound 加密拒绝解密")

                    self._last_status = AuthStatus(
                        valid=False,
                        message=(
                            f"【提取解密失败】\n\n"
                            f"受到 {browser_display} 最新的底层加密机制 (App-Bound Encryption) 限制，"
                            "目前第三方工具无法直接解密提取它的 Cookie。\n\n"
                            "请执行以下任一替代方案：\n"
                            "1. 切换到不受此限制的浏览器 (推荐: Firefox 或 LibreWolf)\n"
                            '2. 使用「手动导入」方式 (前往设置选择"手动导入 cookies.txt"并提供导出的文件)'
                        ),
                    )
                    # 返回 None 意味着失败，不再抛出异常触发管理员提权弹窗
                    return None

                else:
                    # 原本的安全降级，可能只需要管理员权限
                    logger.info("检测到 Chrome v130+ App-Bound 加密，需要管理员权限")
                    self._last_status = AuthStatus(
                        valid=False,
                        message=f"{browser_display} 需要管理员权限才能提取 Cookie（App-Bound 加密）",
                    )
                    raise PermissionError(
                        f"{browser_display} v130+ 使用了 App-Bound 加密。\n"
                        "需要以管理员身份重新启动程序才能提取 Cookie。\n\n"
                        "建议：使用 Edge 或 Firefox 浏览器可避免此问题。"
                    ) from e
            else:
                # 非 App-Bound / 解密相关的其他错误，直接抛出
                raise

        if not cookies:
            browser_display = self.current_source_display
            self._last_status = AuthStatus(
                valid=False,
                message=(
                    f"无法从 {browser_display} 提取 Cookie\n\n"
                    "可能的原因：\n"
                    f"1. {browser_display} 未安装\n"
                    f"2. 未在 {browser_display} 中登录 YouTube\n"
                    f"3. {browser_display} 正在运行（Cookie 数据库被锁定）\n\n"
                    "建议：\n"
                    "• 确保已在浏览器中登录 YouTube\n"
                    "• 完全关闭浏览器后重试\n"
                    "• 尝试使用其他浏览器（如 Edge）"
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

    def _extract_centbrowser_manual(
        self, key_path: str, db_path: str, domains: list[str]
    ) -> list[dict]:
        import base64
        import ctypes
        import ctypes.wintypes
        import json
        import sqlite3

        try:
            from yt_dlp.aes import aes_gcm_decrypt_and_verify
            from yt_dlp.utils import bytes_to_intlist, intlist_to_bytes
        except ImportError as e:
            raise RuntimeError(f"未能导入 yt-dlp 依赖项，CentBrowser 提取需要：{e}") from e

        # DPAPI hook
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        def dpapi_decrypt(encrypted_data: bytes) -> bytes | None:
            CryptUnprotectData = ctypes.windll.crypt32.CryptUnprotectData
            CryptUnprotectData.argtypes = [
                ctypes.POINTER(DATA_BLOB),
                ctypes.POINTER(ctypes.c_wchar_p),
                ctypes.POINTER(DATA_BLOB),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.wintypes.DWORD,
                ctypes.POINTER(DATA_BLOB),
            ]  # pyright: ignore
            CryptUnprotectData.restype = ctypes.wintypes.BOOL
            blob_in = DATA_BLOB()
            blob_in.cbData = len(encrypted_data)
            blob_in.pbData = ctypes.cast(
                ctypes.c_char_p(encrypted_data), ctypes.POINTER(ctypes.c_char)
            )
            blob_out = DATA_BLOB()
            if CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            ):
                out_data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                return out_data
            return None

        with open(key_path, encoding="utf-8") as f:
            local_state = json.load(f)

        encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
        decrypted_key = dpapi_decrypt(encrypted_key[5:])
        if not decrypted_key:
            raise RuntimeError("DPAPI 解密失败 (可能当前非提取设备，或需要特定的系统支持)")

        cookies = []
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                "SELECT host_key, name, path, encrypted_value, is_secure, expires_utc, is_httponly, samesite FROM cookies"
            )

            for row in cursor.fetchall():
                host_key = row["host_key"]
                if domains and not any(host_key.endswith(d) for d in domains):
                    continue

                encrypted_value = row["encrypted_value"]
                if not encrypted_value or not encrypted_value.startswith(b"v10"):
                    continue

                nonce = encrypted_value[3:15]
                ciphertext = encrypted_value[15:-16]
                tag = encrypted_value[-16:]

                try:
                    pt_ints = aes_gcm_decrypt_and_verify(
                        bytes_to_intlist(ciphertext),
                        bytes_to_intlist(decrypted_key),
                        bytes_to_intlist(tag),
                        bytes_to_intlist(nonce),
                    )
                    plaintext_bytes = intlist_to_bytes(pt_ints)

                    if len(plaintext_bytes) > 32 and not plaintext_bytes.isascii():
                        try:
                            plaintext = plaintext_bytes[32:].decode("utf-8")
                        except UnicodeDecodeError:
                            continue
                    else:
                        plaintext = plaintext_bytes.decode("utf-8", errors="ignore")

                    cookies.append(
                        {
                            "domain": host_key,
                            "name": row["name"],
                            "path": row["path"],
                            "value": plaintext,
                            "secure": bool(row["is_secure"]),
                            "expires": int(max(0, (row["expires_utc"] / 1000000) - 11644473600))
                            if row["expires_utc"]
                            else 0,
                            "http_only": bool(row["is_httponly"]),
                            "same_site": row["samesite"] if "samesite" in row.keys() else 0,
                        }
                    )
                except Exception:
                    continue

        return cookies

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
        logger.debug(f"已生成 Cookie 文件: {output_path}")

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
                    "message": f"Cookie 不完整，缺少: {', '.join(missing)}",
                }
            return {
                "valid": True,
                "message": "已验证 (检测到 YouTube 登录)",
            }
        else:
            if valid_cookies:
                return {"valid": True, "message": f"找到 {len(valid_cookies)} 个有效 Cookie"}
            return {"valid": False, "message": "未找到有效 Cookie"}

    def _detect_account_hint(self, cookies: list[dict]) -> str | None:
        """尝试检测账户信息"""
        # 检查是否有 Premium 相关标识
        for c in cookies:
            name = c.get("name", "").lower()
            value = c.get("value", "").lower()
            if "premium" in name or "premium" in value:
                return "YouTube Premium"
        return None

    def _update_status_from_file(self, file_path: str) -> None:
        """从文件更新状态"""
        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="replace")
            cookies = self._parse_netscape_cookies(content)
            validation = self._validate_cookies(cookies, "youtube")

            self._last_status = AuthStatus(
                valid=validation["valid"],
                message=validation["message"],
                cookie_count=len(cookies),
                last_updated=datetime.fromtimestamp(Path(file_path).stat().st_mtime).isoformat(),
            )
        except Exception as e:
            self._last_status = AuthStatus(valid=False, message=f"读取失败: {e}")

    # ==================== 配置持久化 ====================

    def _save_config(self) -> None:
        """保存配置"""
        data = {
            "version": 3,
            "source": self._current_source.value,
            "file_path": self._current_file_path,
            "auto_refresh": self._auto_refresh,
            "current_dle_account_id": self._current_dle_account_id,
            "updated_at": datetime.now().isoformat(),
        }
        with open(self._config_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))

    def _load_config(self) -> None:
        """加载配置"""
        if not self._config_path.exists():
            # 默认使用 Edge 浏览器
            self._current_source = AuthSourceType.EDGE
            self._auto_refresh = True
            logger.info("首次启动，默认使用 Edge 浏览器验证")
            self._save_config()
            return

        try:
            with open(self._config_path, encoding="utf-8") as f:
                data = json.load(f)
            source_value = data.get("source", "edge")  # 默认 edge
            # 如果是 none，自动切换为 edge
            if source_value == "none":
                source_value = "edge"
            self._current_source = AuthSourceType(source_value)
            self._current_file_path = data.get("file_path")
            self._auto_refresh = data.get("auto_refresh", True)
            self._current_dle_account_id = data.get("current_dle_account_id")
            logger.info(f"已加载验证配置: {self.current_source_display}")

            # 尝试恢复上次的验证状态
            self._restore_last_status()
        except Exception as e:
            logger.error(f"加载验证配置失败: {e}")
            # 加载失败时使用默认的 Edge
            self._current_source = AuthSourceType.EDGE

    def _get_dle_cache_file(self, platform: str = "youtube") -> Path:
        """获取当前 DLE 账号对应的缓存文件路径"""
        self._ensure_current_dle_account_valid()
        account = self.current_dle_account
        if account and account.cached_cookie_path:
            return Path(account.cached_cookie_path)

        # 兜底：兼容老路径
        return self.cache_dir / f"cached_dle_{platform}.txt"

    def _build_dle_account_paths(
        self, account_id: str, platform: str = "youtube"
    ) -> tuple[Path, Path]:
        """构建 DLE 账号 profile 与缓存路径"""
        root = self._dle_accounts_dir / account_id
        profile_dir = root / "profile"
        cache_file = root / "cookies.txt"
        profile_dir.mkdir(parents=True, exist_ok=True)
        return profile_dir, cache_file

    def _mark_current_dle_account_refreshed(
        self,
        cache_file: Path,
        cookie_count: int,
        valid: bool,
    ) -> None:
        """刷新后回写当前 DLE 账号状态"""
        account = self.current_dle_account
        if not account:
            return

        account.cached_cookie_path = str(cache_file)
        account.last_extracted_at = datetime.now().isoformat()
        account.cookie_count = cookie_count
        account.valid = valid
        self._save_dle_accounts()

    # ==================== DLE 多账号管理 ====================

    def list_dle_accounts(self, platform: str = "youtube") -> list[DLEAccount]:
        """列出 DLE 账号"""
        self._load_dle_accounts()
        return [a for a in self._dle_accounts.values() if a.platform == platform]

    def create_dle_account(
        self,
        display_name: str,
        platform: str = "youtube",
        notes: str | None = None,
    ) -> DLEAccount:
        """创建 DLE 账号"""
        account_id = uuid4().hex
        profile_dir, cache_file = self._build_dle_account_paths(account_id, platform)
        account = DLEAccount(
            account_id=account_id,
            display_name=display_name.strip() or "未命名账号",
            platform=platform,
            profile_dir=str(profile_dir),
            cached_cookie_path=str(cache_file),
            is_default=(len(self._dle_accounts) == 0),
            notes=notes,
        )
        self._dle_accounts[account_id] = account

        if not self._current_dle_account_id:
            self._current_dle_account_id = account_id
            self._save_config()

        self._save_dle_accounts()
        return account

    def update_dle_account(
        self,
        account_id: str,
        *,
        display_name: str | None = None,
        notes: str | None = None,
        is_default: bool | None = None,
    ) -> bool:
        """更新 DLE 账号元信息"""
        account = self._dle_accounts.get(account_id)
        if not account:
            return False

        if display_name is not None:
            account.display_name = display_name.strip() or account.display_name
        if notes is not None:
            account.notes = notes
        if is_default is True:
            for a in self._dle_accounts.values():
                a.is_default = False
            account.is_default = True

        self._save_dle_accounts()
        return True

    def delete_dle_account(self, account_id: str, remove_storage: bool = False) -> bool:
        """删除 DLE 账号"""
        account = self._dle_accounts.get(account_id)
        if not account:
            return False

        if len(self._dle_accounts) <= 1:
            logger.warning("至少需要保留一个 DLE 账号，拒绝删除")
            return False

        self._dle_accounts.pop(account_id, None)

        if remove_storage:
            account_root = self._dle_accounts_dir / account_id
            try:
                shutil.rmtree(account_root, ignore_errors=True)
            except Exception as e:
                logger.warning(f"删除 DLE 账号存储目录失败: {e}")

        if self._current_dle_account_id == account_id:
            self._current_dle_account_id = next(iter(self._dle_accounts.keys()), None)
            self._save_config()

        # 保证始终有一个默认账号
        if not any(a.is_default for a in self._dle_accounts.values()):
            first = next(iter(self._dle_accounts.values()), None)
            if first:
                first.is_default = True

        self._save_dle_accounts()
        return True

    def set_current_dle_account(self, account_id: str) -> bool:
        """设置当前激活 DLE 账号"""
        if account_id not in self._dle_accounts:
            return False
        self._current_dle_account_id = account_id
        self._save_config()

        # 按用户预期：切换 DLE 账号时，立即将该账号 Cookie 同步到统一 bin/cookies.txt
        self._sync_current_dle_cookie_to_unified_cookiefile()
        return True

    def _sync_current_dle_cookie_to_unified_cookiefile(self) -> bool:
        """将当前 DLE 账号的 Cookie 覆盖同步到统一 bin/cookies.txt"""
        account = self.current_dle_account
        if not account or not account.cached_cookie_path:
            return False

        src = Path(account.cached_cookie_path)
        if not src.exists():
            logger.info("当前 DLE 账号尚无 Cookie 缓存，跳过同步到 bin/cookies.txt")
            return False

        try:
            from fluentytdl.auth.cookie_sentinel import cookie_sentinel

            cookie_sentinel.cookie_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, cookie_sentinel.cookie_path)
            self._update_status_from_file(str(src))
            cookie_sentinel._save_meta(f"dle:{account.account_id}", self._last_status.cookie_count)
            logger.info(
                f"已切换到 DLE 账号 {account.display_name}，并同步 Cookie 到 {cookie_sentinel.cookie_path}"
            )
            return True
        except Exception as e:
            logger.warning(f"同步当前 DLE 账号 Cookie 到统一文件失败: {e}")
            return False

    def _save_dle_accounts(self) -> None:
        """保存 DLE 账号配置"""
        data = {
            "version": 1,
            "accounts": [a.to_dict() for a in self._dle_accounts.values()],
            "updated_at": datetime.now().isoformat(),
        }
        with open(self._dle_accounts_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))

    def _load_dle_accounts(self) -> None:
        """加载 DLE 账号配置"""
        self._dle_accounts = {}
        old_accounts_path = self.cache_dir / "dle_accounts.json"
        path_to_load = (
            self._dle_accounts_path if self._dle_accounts_path.exists() else old_accounts_path
        )
        if not path_to_load.exists():
            return
        try:
            with open(path_to_load, encoding="utf-8") as f:
                data = json.load(f)

            for raw in data.get("accounts", []):
                acc = DLEAccount.from_dict(raw)
                if not acc.account_id:
                    continue
                # 将账号目录统一迁移到 bin/dle_user/<account_id>/ 下
                profile_dir, cache_file = self._build_dle_account_paths(
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
                    logger.warning(f"迁移账号 {acc.account_id} Cookie 文件失败: {e}")

                # 路径统一写回新结构
                acc.profile_dir = str(profile_dir)
                acc.cached_cookie_path = str(cache_file)
                self._dle_accounts[acc.account_id] = acc

            # 若是从旧位置加载，落盘到新位置
            if path_to_load != self._dle_accounts_path:
                self._save_dle_accounts()
        except Exception as e:
            logger.error(f"加载 DLE 账号配置失败: {e}")

    def _ensure_current_dle_account_valid(self) -> None:
        """确保当前激活 DLE 账号存在"""
        # 没有任何账号时创建默认账号
        if not self._dle_accounts:
            default = self.create_dle_account("默认账号", platform="youtube")
            default.is_default = True
            self._save_dle_accounts()

        if self._current_dle_account_id in self._dle_accounts:
            return

        # 优先默认账号，其次第一个
        default = next((a for a in self._dle_accounts.values() if a.is_default), None)
        chosen = default or next(iter(self._dle_accounts.values()), None)
        if chosen:
            self._current_dle_account_id = chosen.account_id
            self._save_config()

    def _migrate_legacy_dle_cache_if_needed(self) -> None:
        """将旧单账号 DLE 缓存迁移到默认账号"""
        legacy = self.cache_dir / "cached_dle_youtube.txt"
        if not legacy.exists():
            return

        # 已经迁移过（存在账号化缓存）则不再处理
        has_account_cache = any(
            Path(a.cached_cookie_path).exists()
            for a in self._dle_accounts.values()
            if a.cached_cookie_path
        )
        if has_account_cache:
            return

        self._ensure_current_dle_account_valid()
        account = self.current_dle_account
        if not account:
            return

        try:
            target = Path(account.cached_cookie_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, target)
            account.last_extracted_at = datetime.now().isoformat()
            account.valid = True
            self._save_dle_accounts()
            logger.info(f"已将旧 DLE 缓存迁移到账号 {account.display_name}: {target}")

            # 若当前正在 DLE 模式，迁移后同步到统一 cookiefile
            if self._current_source == AuthSourceType.DLE:
                self._sync_current_dle_cookie_to_unified_cookiefile()
        except Exception as e:
            logger.warning(f"迁移旧 DLE 缓存失败: {e}")

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
                        self._last_status.message += " (缓存可能过期)"
        except Exception as e:
            logger.debug(f"恢复状态失败: {e}")

    def startup_refresh(self) -> AuthStatus:
        """
        启动时自动刷新 Cookie

        在应用启动时调用，自动获取 Cookie 并验证有效性。

        Returns:
            刷新后的状态
        """
        if self._current_source == AuthSourceType.NONE:
            # 如果当前是 NONE，切换到默认的 Edge
            self._current_source = AuthSourceType.EDGE
            self._save_config()

        if self._current_source == AuthSourceType.FILE:
            # 文件模式不自动刷新，只检查文件
            if self._current_file_path and Path(self._current_file_path).exists():
                self._update_status_from_file(self._current_file_path)
            else:
                self._last_status = AuthStatus(valid=False, message="Cookie 文件不存在，请重新选择")
            return self._last_status

        # 浏览器模式：自动刷新
        logger.info(f"启动时自动刷新 Cookie ({self.current_source_display})...")
        try:
            cookie_path = self.get_cookie_file_for_ytdlp("youtube", force_refresh=True)
            if cookie_path:
                logger.info(f"Cookie 刷新成功: {self._last_status.message}")
            else:
                logger.warning(f"Cookie 刷新失败: {self._last_status.message}")
        except Exception as e:
            logger.error(f"启动刷新失败: {e}")
            self._last_status = AuthStatus(valid=False, message=f"刷新失败: {e}")

        return self._last_status

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
            logger.error(f"加载配置文件失败: {e}")

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
