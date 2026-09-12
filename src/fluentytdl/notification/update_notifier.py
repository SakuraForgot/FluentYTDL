"""把「有更新可用」这件事送进消息中心（标题栏小铃铛）。

以前更新提醒全靠各张设置卡片自己弹 InfoBar，问题有三个：

1. 启动时 5 个组件 + app-core 的检查几乎同时回来，主窗口顶部会一次炸出
   5~6 条 InfoBar，把界面糊住。
2. 「已是最新」也在里面 —— 这是纯噪音，用户什么都不用做。
3. `app_update_available` 同时被主窗口和设置页卡片监听，同一件事弹两条。

现在统一成：

* **自动（启动/定时）检查** —— 一条 InfoBar 都不弹；只有真的有更新才写进消息中心，
  用小铃铛上的未读徽章提示。
* **手动检查** —— 卡片自己弹一条 InfoBar 做即时反馈，同时也写进消息中心留档。

同一个 (组件, 版本) 在一个会话里只写一次，避免用户连点「检查更新」刷出重复条目。
"""

from __future__ import annotations

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text

from ..utils.logger import logger
from .notification_center import notification_center
from .notification_model import Notification

#: 已经写进消息中心的 (组件, 版本)，用于会话内去重。
_notified: set[tuple[str, str]] = set()

_installed = False


def _push(component: str, version: str, title: str, message: str) -> None:
    """写入一条更新通知，同一个 (组件, 版本) 只写一次。"""
    fingerprint = (component, str(version))
    if fingerprint in _notified:
        return
    _notified.add(fingerprint)

    notification_center.push(
        Notification(
            type="update_available",
            title=title,
            message=message,
            severity="info",
            metadata={"component": component, "version": str(version)},
        )
    )


def notify_app_update(info: dict) -> None:
    """app-core 有新版本。"""
    version = info.get("version", "?")
    is_pre = info.get("is_prerelease", False)
    prefix = tr_text("预发布版本") if is_pre else tr_text("新版本")
    _push(
        "app-core",
        version,
        tr_text("FluentYTDL {0} {1}", prefix, version),
        tr_text("{0} {1} 已可用，前往「设置 → 更新」即可更新。", prefix, version),
    )


def notify_component_update(key: str, result: dict) -> None:
    """bin/ 下的外部组件（yt-dlp / FFmpeg / Deno / POT Provider）有新版本。"""
    if not result.get("update_available"):
        return

    latest = result.get("latest", "?")
    current = result.get("current", "unknown")
    if latest in ("", "unknown"):
        # 检查失败（拿不到最新版本号）不算「有更新」，别往铃铛里塞噪音
        return

    # 没有下载地址就装不上 —— 推一条点了也没用的提醒只会消耗用户的信任。
    # 这条曾经真的发生过：清单里 bin 组件的 url 全是空串，铃铛一直提示 deno 有更新，
    # 点进去只会得到一句"没能解析出下载地址"。
    if not result.get("url"):
        log_text(logger, "debug", "[UpdateNotifier] 跳过 {0}：有更新但没有下载地址", key)
        return

    try:
        from ..core.dependency_manager import dependency_manager

        name = dependency_manager.components[key].name
    except (ImportError, KeyError):
        name = key

    current_ch = str(result.get("current_channel") or "")
    latest_ch = str(result.get("latest_channel") or "")
    if current_ch and latest_ch and current_ch != latest_ch and current != "unknown":
        # 频道切换不是"升级"，版本号可能反而更小，说成"新版本"会让人困惑
        title = tr_text("{0} 需要切换频道", name)
        message = tr_text(
            "{0} 将从 {1} 频道切换到 {2} 频道（{3} → {4}），前往「设置 → 更新」即可应用。",
            name,
            current_ch,
            latest_ch,
            current,
            latest,
        )
    else:
        title = tr_text("{0} 有新版本 {1}", name, latest)
        message = tr_text(
            "{0} {1} 已可用（当前 {2}），前往「设置 → 更新」即可更新。", name, latest, current
        )

    if result.get("source") == "path":
        message = tr_text(
            "{0}{1}",
            message,
            tr_text("\n当前使用的是系统 PATH 上的版本，更新会在应用自带目录下安装一份并优先使用。"),
        )

    _push(key, latest, title, message)


def install_update_notifier() -> None:
    """把两个更新管理器接到消息中心上。重复调用无副作用。"""
    global _installed
    if _installed:
        return
    _installed = True

    from ..core.component_update_manager import component_update_manager
    from ..core.dependency_manager import dependency_manager

    component_update_manager.app_update_available.connect(notify_app_update)
    dependency_manager.check_finished.connect(notify_component_update)
    log_text(logger, "debug", "[UpdateNotifier] 更新通知已接入消息中心")
