"""
FluentYTDL 认证功能域

包含浏览器 Cookie 提取、验证状态管理、Cookie 生命周期管理等功能。
"""

from .auth_service import (
    ADMIN_REQUIRED_BROWSERS,
    BROWSER_COMBO_ITEMS,
    BROWSER_COMBO_LABELS,
    BROWSER_SOURCES,
    LEGACY_SOURCES,
    AuthProfile,
    AuthService,
    AuthSourceType,
    AuthStatus,
    WebView2Account,
    auth_service,
    browser_combo_index,
    browser_source_at,
    is_admin,
)
from .cookie_sentinel import CookieSentinel, cookie_sentinel

__all__ = [
    # 认证服务
    "AuthService",
    "auth_service",
    "AuthSourceType",
    "AuthStatus",
    "AuthProfile",
    "WebView2Account",
    "BROWSER_SOURCES",
    "ADMIN_REQUIRED_BROWSERS",
    # 浏览器下拉框的唯一顺序来源（UI 层不要再手写位置列表）
    "BROWSER_COMBO_ITEMS",
    "BROWSER_COMBO_LABELS",
    "browser_source_at",
    "browser_combo_index",
    "LEGACY_SOURCES",
    "is_admin",
    # Cookie 哨兵
    "CookieSentinel",
    "cookie_sentinel",
]
