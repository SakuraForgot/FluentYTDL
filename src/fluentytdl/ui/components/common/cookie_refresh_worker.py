"""Cookie 刷新后台线程 —— 全 UI 层唯一允许调用 `force_refresh_with_uac()` 的地方。

`cookie_sentinel.force_refresh_with_uac()` 在 WebView2 模式下会拉起子进程并**同步**
等待登录窗口关闭（缺 WebView2 Runtime 时最长数十秒），浏览器模式下要读被锁的 DPAPI
数据库。任何一条都足以让 Qt 主线程假死 —— 界面不重绘、点什么都没反应，用户看到的就是
"点了登录之后卡死"。所以刷新只能发生在 QThread 里，结果靠 `finished` 信号回主线程。

以前这个类只住在 `settings_page.py`，于是 `selection_dialog` / `reimagined_main_window`
各自在主线程裸调 `force_refresh_with_uac()`。搬到 common 层是为了让它们能复用，而不是
让 dialog 去 import 一个 page。
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text


class CookieRefreshWorker(QThread):
    """Cookie 刷新工作线程（Qt 线程，打包后可靠）"""

    finished = Signal(bool, str, bool)  # (成功标志, 消息, 是否需要管理员权限)

    def __init__(self, parent=None, platform: str | None = None):
        super().__init__(parent)
        #: None 表示两个平台一起刷（浏览器提取一次就能覆盖两边）；
        #: 传具体平台时只占用该平台的互斥锁，另一个平台的刷新不会被拒。
        self.platform = platform

    def run(self):
        """在 Qt 线程中执行 Cookie 刷新"""
        from ....auth.auth_service import auth_service
        from ....auth.cookie_sentinel import cookie_sentinel
        from ....utils.logger import logger

        success = False
        message = tr_text("未知错误")

        try:
            # 直接刷新（调用前已检查权限，或已是管理员/非 Edge）
            success, message = cookie_sentinel.force_refresh_with_uac(platform=self.platform)

            if not success:
                # 获取详细状态
                status = auth_service.last_status
                if status and hasattr(status, "message") and status.message:
                    message = status.message

                # 友好的错误引导
                browser_name = auth_service.current_source_display

                # 如果 auth_service 已经提供了关于【提取解密失败】的详细多行指引，则保留其内容
                # 否则，如果是其他诸如"未找到文件"或普通的异常，才覆盖为通用建议
                if "【提取解密失败】" not in message and (
                    "未找到" in message or "not found" in message.lower()
                ):
                    message = (
                        tr_text("无法从 {0} 提取 Cookie\n\n", browser_name)
                        + self.tr("可能的原因：\n")
                        + tr_text(
                            "1. {0} 未安装或未登录相关平台\n2. {1} Cookie 数据库被锁定（请关闭浏览器）\n\n",
                            browser_name,
                            browser_name,
                        )
                        + self.tr("建议：完全关闭浏览器后重试")
                    )

                log_text(logger, "warning", "[CookieRefreshWorker] 提取失败: {0}", message)
        except Exception as e:
            success = False
            message = tr_text("刷新异常: {0}", str(e))
            log_text(logger, "exception", "[CookieRefreshWorker] 异常")

        # 发射信号（线程安全，第三个参数保留但不再使用）
        self.finished.emit(success, message, False)
