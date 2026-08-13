from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QWidget
from qfluentwidgets import InfoBar, InfoBarPosition


def tr(text: str) -> str:
    return QCoreApplication.translate("FixRegistry", text)


def do_relogin(parent_widget: QWidget) -> None:
    """处理重登录（例如打开 WebView2 登录对话框）"""
    # 尝试寻找主窗口并调用其 switch_to_settings 或类似方法
    main_win = parent_widget.window()
    settings_iface = getattr(main_win, "settings_interface", None)
    if settings_iface is not None:
        if hasattr(main_win, "switchTo"):
            main_win.switchTo(settings_iface)  # type: ignore
        InfoBar.info(
            tr("提示"),
            tr("请在设置页中重新提取或验证您的账号 Cookie。"),
            parent=main_win,
            position=InfoBarPosition.TOP,
            duration=5000,
        )
    else:
        InfoBar.warning(
            tr("不支持的操作"),
            tr("无法定位到设置界面。"),
            parent=main_win,
            position=InfoBarPosition.TOP,
            duration=3000,
        )


def extract_cookie(parent_widget: QWidget) -> None:
    """提取 Cookie 修复动作"""
    do_relogin(parent_widget)


def switch_proxy(parent_widget: QWidget) -> None:
    """切换代理修复动作"""
    main_win = parent_widget.window()
    settings_iface = getattr(main_win, "settings_interface", None)
    if settings_iface is not None and hasattr(main_win, "switchTo"):
        main_win.switchTo(settings_iface)  # type: ignore
        InfoBar.info(
            tr("网络设置"),
            tr("请在此配置可用的代理节点。"),
            parent=main_win,
            position=InfoBarPosition.TOP,
            duration=5000,
        )


def change_download_dir(parent_widget: QWidget) -> None:
    """更改下载目录"""
    main_win = parent_widget.window()
    settings_iface = getattr(main_win, "settings_interface", None)
    if settings_iface is not None and hasattr(main_win, "switchTo"):
        main_win.switchTo(settings_iface)  # type: ignore
        InfoBar.info(
            tr("存储设置"),
            tr("请更改默认的下载保存路径。"),
            parent=main_win,
            position=InfoBarPosition.TOP,
            duration=5000,
        )


def update_component(parent_widget: QWidget) -> None:
    """更新核心组件"""
    main_win = parent_widget.window()
    settings_iface = getattr(main_win, "settings_interface", None)
    if settings_iface is not None and hasattr(main_win, "switchTo"):
        main_win.switchTo(settings_iface)  # type: ignore
        InfoBar.info(
            tr("组件更新"),
            tr("请在设置页中检查并更新 yt-dlp 核心组件。"),
            parent=main_win,
            position=InfoBarPosition.TOP,
            duration=5000,
        )


def refresh_pot(parent_widget: QWidget) -> None:
    """重建 POT 验证引擎（Bot 检测 / POT 令牌缺失时的修复动作）。

    ``pot_manager.try_recover()`` 最坏要跑十几秒（清缓存 → 重置 IT → 重启 → 验证铸币），
    绝不能在 UI 线程直接调用，所以这里复用设置页里已经做好线程封装的诊断入口。
    """
    main_win = parent_widget.window()
    settings_iface = getattr(main_win, "settings_interface", None)
    if settings_iface is None:
        InfoBar.warning(
            tr("不支持的操作"),
            tr("无法定位到设置界面。"),
            parent=main_win,
            position=InfoBarPosition.TOP,
            duration=3000,
        )
        return

    switch_to = getattr(main_win, "switchTo", None)
    if callable(switch_to):
        switch_to(settings_iface)

    starter = getattr(settings_iface, "_start_pot_diagnose", None)
    if callable(starter):
        starter(recover=True)
        return

    InfoBar.info(
        tr("POT 验证引擎"),
        tr("请在设置页中点击 POT 验证引擎的「检测」按钮完成修复。"),
        parent=main_win,
        position=InfoBarPosition.TOP,
        duration=5000,
    )


def retry_now(parent_widget: QWidget) -> None:
    """立即重试：优先唤醒挂起中的 worker，否则重建任务。"""
    worker = getattr(parent_widget, "worker", None)
    if worker is not None and getattr(worker, "is_suspended", False):
        resume = getattr(worker, "resume_suspension", None)
        if callable(resume):
            resume("retry")
            return

    retry = getattr(parent_widget, "_retry_download", None)
    if callable(retry):
        retry()
        return

    InfoBar.info(
        tr("请手动重试"),
        tr("请点击任务卡片上的重试按钮重新开始下载。"),
        parent=parent_widget.window(),
        position=InfoBarPosition.TOP,
        duration=4000,
    )


def open_download_dir(parent_widget: QWidget) -> None:
    """打开下载目录（磁盘空间不足、文件名过长等需要用户去现场处理的场景）。"""
    import os

    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    from ....core.config_manager import config_manager

    target = getattr(getattr(parent_widget, "worker", None), "download_dir", None)
    if not (isinstance(target, str) and target.strip()):
        target = str(config_manager.get("download_dir") or "").strip()
    if not target or not os.path.isdir(target):
        InfoBar.warning(
            tr("目录不存在"),
            tr("找不到下载目录，请到设置页确认保存路径。"),
            parent=parent_widget.window(),
            position=InfoBarPosition.TOP,
            duration=4000,
        )
        return

    QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(target)))


FIX_ACTIONS = {
    "relogin": do_relogin,
    "extract_cookie": extract_cookie,
    "switch_proxy": switch_proxy,
    "change_download_dir": change_download_dir,
    "update_component": update_component,
    "refresh_pot": refresh_pot,
    "retry_now": retry_now,
    "open_download_dir": open_download_dir,
}


def execute_fix_action(action_id: str, parent_widget: QWidget) -> bool:
    """执行修复动作"""
    action_func = FIX_ACTIONS.get(action_id)
    if action_func:
        try:
            action_func(parent_widget)
            return True
        except Exception as e:
            InfoBar.error(
                tr("执行失败"),
                f"尝试执行自动修复时发生错误: {e}",
                parent=parent_widget.window(),
                position=InfoBarPosition.TOP,
            )
            return False
    return False
