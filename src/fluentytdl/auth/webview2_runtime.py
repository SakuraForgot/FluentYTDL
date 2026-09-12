"""
WebView2 运行时探测

WebView2 登录模式依赖 Microsoft Edge WebView2 Runtime。它在绝大多数 Win10/11 上
随 Edge 预装，但**不是必然存在**：精简版系统镜像、企业策略移除 Edge、Server 版
Windows 都可能没有它。

这个模块存在的唯一理由是**在启动子进程之前**回答"装了没有"。没有它的时候，
pywebview 的 `webview.start()` 会在子进程里抛异常并让子进程静默死亡，而父进程
正阻塞在 `cookie_queue.get(timeout=330)` 上 —— 用户看到的就是 5.5 分钟的界面卡死。
预检把这条路径变成一句可操作的提示。

探测走注册表而不是找文件：Evergreen Runtime 的安装目录带版本号且随自动更新变化，
而 EdgeUpdate 的 `pv` 值是微软官方文档指定的检测方式。
"""

from __future__ import annotations

import sys

from fluentytdl.utils.localized_log import log_text

from ..utils.logger import logger

#: WebView2 Runtime 的固定 GUID（Evergreen 与 Fixed Version 共用）。
#: 来自微软官方文档，不要改。
_WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

#: 三个候选注册表位置，按命中概率排序：
#:   1. 64 位系统上 32 位安装的 EdgeUpdate（最常见）
#:   2. 原生位宽（ARM64 / 纯 32 位系统）
#:   3. 用户级安装（非管理员装的 Runtime）
_REGISTRY_PATHS: list[tuple[str, str]] = [
    (
        "HKEY_LOCAL_MACHINE",
        rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT_GUID}",
    ),
    ("HKEY_LOCAL_MACHINE", rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT_GUID}"),
    ("HKEY_CURRENT_USER", rf"Software\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT_GUID}"),
]

WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"

#: 探测结果缓存。Runtime 不会在进程生命周期内被装上又卸掉，查一次就够；
#: 而登录按钮点击是热路径，不该每次都开注册表。
_cached_result: tuple[bool, str | None] | None = None


def is_webview2_runtime_available(use_cache: bool = True) -> tuple[bool, str | None]:
    """探测 WebView2 Runtime 是否可用。

    Args:
        use_cache: 是否使用进程级缓存。用户刚装完 Runtime 想重试时传 False。

    Returns:
        (是否可用, 版本号)。版本号仅在可用时有意义，可能为 None（读到了键但没有 pv）。
    """
    global _cached_result

    if use_cache and _cached_result is not None:
        return _cached_result

    result = _probe()

    if result[0]:
        log_text(logger, "info", "[WebView2Runtime] 已检测到运行时，版本 {0}", result[1])
    else:
        log_text(logger, "warning", "[WebView2Runtime] 未检测到 WebView2 运行时，登录模式不可用")

    _cached_result = result
    return result


def _probe() -> tuple[bool, str | None]:
    """实际的注册表查询。任何异常都当作"不可用"处理。"""
    if sys.platform != "win32":
        # WebView2 是 Windows 独占；其它平台上 pywebview 走 GTK/Cocoa，
        # 但本项目的登录流程只在 Windows 上验证过，保守返回不可用。
        return False, None

    try:
        import winreg
    except ImportError:
        return False, None

    for hive_name, subkey in _REGISTRY_PATHS:
        hive = getattr(winreg, hive_name)
        try:
            with winreg.OpenKey(hive, subkey) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            # 键不存在 / 无权限读 —— 换下一个候选
            continue
        except Exception:
            continue

        # 微软文档明确：`pv` 为 "0.0.0.0" 表示占位项，Runtime 实际未安装。
        if isinstance(version, str) and version.strip() and version.strip() != "0.0.0.0":
            return True, version.strip()

    return False, None
