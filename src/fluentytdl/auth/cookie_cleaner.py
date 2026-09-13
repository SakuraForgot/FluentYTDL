"""
Cookie 清洗与合规过滤模块

负责对提取的 Cookie 进行隐私合规清洗，仅保留特定平台运行所需的最小化 Cookie 集合。

**平台参数必须传对。** `platform="youtube"` 会套上 `YOUTUBE_ALLOWED_NAMES` 名字白名单，
而 X 的 `auth_token` / `ct0` 不在里面 —— 拿 X 的 Cookie 走 youtube 分支，结果是空列表。
twitter 分支 `allowed_names = None`（不做名字过滤），所以清洗逻辑本身不会吃掉 X 的
Cookie，风险 100% 来自调用方传错 platform。`tests/test_cookie_truth_source.py` 两条
测试就是这条语义的护栏。
"""

from __future__ import annotations

import time
from typing import Any

from fluentytdl.utils.localized_log import log_text

from ..utils.logger import logger


class CookieCleaner:
    """Cookie 清洗器"""

    # 平台域名白名单
    PLATFORM_DOMAINS = {
        "youtube": {".youtube.com", "youtube.com", ".google.com", "google.com"},
        "bilibili": {".bilibili.com", "bilibili.com"},
        "twitter": {".twitter.com", ".x.com", "twitter.com", "x.com"},
    }

    # YouTube 核心 Cookie 白名单 (严格匹配)
    YOUTUBE_ALLOWED_NAMES = {
        # 身份认证
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        # 安全认证 (关键)
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
        # SID 绑定校验令牌 (必需: 缺失会导致 YouTube 判定为未认证)
        "__Secure-1PSIDCC",
        "__Secure-3PSIDCC",
        "SIDCC",
        # 时间戳验证令牌 (yt-dlp 用于会话新鲜度校验)
        "__Secure-1PSIDTS",
        "__Secure-3PSIDTS",
        # 会话状态
        "LOGIN_INFO",
        "YSC",
        # 用户偏好
        "PREF",
        # 设备/访客标识
        "VISITOR_INFO1_LIVE",
        "VISITOR_PRIVACY_METADATA",
        # Google 设备标识
        "NID",
    }

    # 标准 Netscape Cookie 字段
    NETSCAPE_FIELDS = {"domain", "path", "secure", "expires", "name", "value"}

    @staticmethod
    def _domain_allowed(domain: str, allowed_domains: set[str]) -> bool:
        """域名是否落在白名单里：**相等或是其子域**。

        原来的实现是 `any(domain.endswith(d) or d.endswith(domain) ...)`。反向那半
        （`d.endswith(domain)`）过于宽松：`domain=".com"` 能匹配白名单里的 `.x.com`，
        于是任何一个顶级域 Cookie 都能混进真相源。前缀点在两边都先剥掉，`x.com` 与
        `.x.com` 视为同一个域。
        """
        host = domain.lstrip(".").lower()
        if not host:
            return False

        for allowed in allowed_domains:
            base = allowed.lstrip(".").lower()
            if base and (host == base or host.endswith("." + base)):
                return True
        return False

    @classmethod
    def clean(
        cls,
        cookies: list[dict[str, Any]],
        platform: str = "youtube",
        enable_cleaning: bool = True,
        *,
        drop_expired: bool = True,
    ) -> list[dict[str, Any]]:
        """
        清洗 Cookie 列表

        1. 丢弃已过期条目（由 `drop_expired` 控制，**与合规清洗无关**）
        2. 过滤非白名单域名（相等或子域）
        3. (YouTube) 过滤非白名单 Cookie Name
        4. 只保留 Netscape 标准字段

        Args:
            cookies: 原始 Cookie 列表
            platform: 平台标识 (youtube, twitter, bilibili)。**传错会洗掉目标平台的凭证**
            enable_cleaning: 是否启用隐私合规清洗（域名 + 名字白名单）
            drop_expired: 是否丢弃已过期条目。独立于 `enable_cleaning` ——
                以前它挂在合规清洗外面无条件执行，用户关掉"Cookie 清理"后仍然被丢，
                与开关语义不符；现在是一个显式的、可单独关闭的维度，默认仍为 True。

        Returns:
            清洗后的 Cookie 列表
        """
        if not cookies:
            return []

        cleaned = []
        allowed_domains = cls.PLATFORM_DOMAINS.get(platform, set())
        allowed_names = cls.YOUTUBE_ALLOWED_NAMES if platform == "youtube" else None

        ignored_domains: set[str] = set()
        ignored_names: set[str] = set()
        expired_count = 0

        current_time = int(time.time())

        for cookie in cookies:
            # 0. 过期时间检查（expires == 0 是会话 Cookie，不算过期）
            expires = int(cookie.get("expires", 0) or 0)
            if drop_expired and 0 < expires < current_time:
                expired_count += 1
                continue

            # 1. 域名过滤
            domain = cookie.get("domain", "")
            if (
                enable_cleaning
                and allowed_domains
                and not cls._domain_allowed(domain, allowed_domains)
            ):
                ignored_domains.add(domain)
                continue

            # 2. Name 过滤 (仅限 YouTube)
            name = cookie.get("name", "")
            if enable_cleaning and allowed_names is not None and name not in allowed_names:
                ignored_names.add(name)
                continue

            # 3. 字段清洗 (仅保留 Netscape 标准字段)
            #
            # 不再注入 `flag` 键：它不在 NETSCAPE_FIELDS 里，而真正写文件的
            # `auth_service._write_netscape_file()` 自己按 domain 是否以 "." 开头
            # 重算这一列，从来没读过这个键 —— 留着只是噪声。
            clean_cookie = {k: v for k, v in cookie.items() if k in cls.NETSCAPE_FIELDS}
            clean_cookie.setdefault("domain", domain)

            cleaned.append(clean_cookie)

        # 日志记录清洗结果
        if len(cleaned) < len(cookies):
            log_text(
                logger,
                "info",
                "[{0}] Cookie 清洗完成: {1} -> {2} (移除: {3}, 其中已过期 {4} 个)",
                platform,
                len(cookies),
                len(cleaned),
                len(cookies) - len(cleaned),
                expired_count,
            )
            if ignored_domains:
                log_text(logger, "debug", "已过滤域名: {0}等", ", ".join(list(ignored_domains)[:5]))
            if ignored_names:
                log_text(
                    logger, "debug", "已过滤无关 Cookie: {0}等", ", ".join(list(ignored_names)[:10])
                )
        else:
            log_text(logger, "debug", "[{0}] Cookie 清洗完成: 无需过滤", platform)

        return cleaned
