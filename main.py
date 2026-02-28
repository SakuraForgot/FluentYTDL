from __future__ import annotations

import os
import sys
from pathlib import Path

# === 优先检测特殊模式（必须在导入任何GUI库之前） ===

# 检测管理员模式（整个程序以管理员身份运行，用于 Cookie 提取）
IS_ADMIN_MODE = "--admin-mode" in sys.argv
if IS_ADMIN_MODE:
    # 移除 --admin-mode 参数，避免传递给 Qt
    sys.argv = [arg for arg in sys.argv if arg != "--admin-mode"]

# 解决 Windows 下 QtNetwork HTTPS 可能遇到的 OpenSSL DLL 缺失问题
# 注意：需要在导入 PySide6 相关模块之前设置 PATH
if sys.platform == "win32":
    try:
        import PySide6

        package_dir = os.path.dirname(PySide6.__file__)
        openssl_dir = os.path.join(package_dir, "openssl", "bin")
        if os.path.exists(openssl_dir):
            os.environ["PATH"] = openssl_dir + os.pathsep + os.environ.get("PATH", "")
    except (ImportError, OSError):
        pass

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def main() -> None:
    # Ensure "src" is importable when running from repo root
    root_dir = Path(__file__).resolve().parent
    src_dir = root_dir / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    # === 1. 修改缩放策略 (关键) ===
    # 如觉得太小，可改为 1.5；或删除本行让 Qt/系统自动接管。
    os.environ["QT_SCALE_FACTOR"] = "1.0"
    # 备选：启用自动 HighDPI（通常 Qt6 默认就支持，只有在特殊环境下才需要）
    # os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

    # 1. 创建应用
    app = QApplication(sys.argv)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)

    # === 避免强制写死浅色模式，跟随用户配置动态调整 ===
    import qfluentwidgets

    # Needs to be imported before UI but after config is ready
    from fluentytdl.core.config_manager import config_manager

    theme_mode = config_manager.get("theme_mode", "Auto")
    if theme_mode == "Light":
        qfluentwidgets.setTheme(qfluentwidgets.Theme.LIGHT)
    elif theme_mode == "Dark":
        qfluentwidgets.setTheme(qfluentwidgets.Theme.DARK)
    else:
        qfluentwidgets.setTheme(qfluentwidgets.Theme.AUTO)

    # Set application icon from assets/logo.png if available (comprehensive replacement)
    try:
        root_dir = Path(__file__).resolve().parent
        icon_path = root_dir / "assets" / "logo.png"
        if icon_path.exists():
            from PySide6.QtGui import QIcon

            app.setWindowIcon(QIcon(str(icon_path)))
    except Exception:
        pass

    # === 2. 设置全局字体 (关键) ===
    font = QFont("Microsoft YaHei UI", 9)
    try:
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    except Exception:
        pass
    app.setFont(font)

    # Import UI after QApplication is created to avoid triggering Qt font operations
    # during module import (which can cause QFont warnings if done before app exists).
    # from fluentytdl.ui.main_window import MainWindow
    # === 新增预加载界面逻辑 ===
    from PySide6.QtCore import QThread, QTimer, Signal
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from fluentytdl.core.config_manager import config_manager

    class PotInitThread(QThread):
        finished_signal = Signal()

        def run(self):
            import time

            try:
                from loguru import logger

                from fluentytdl.youtube.pot_manager import pot_manager

                for attempt in range(3):
                    if pot_manager.start_server():
                        logger.info("POT Provider 服务已启动")
                        logger.info("POT Provider: 开始预热 BotGuard ...")
                        # 缩短超时设为12秒，避免过长等待
                        ok, msg = pot_manager.verify_token_generation(timeout=12)
                        if ok:
                            logger.info(f"POT Provider: 预热完成 — {msg}")
                        else:
                            logger.warning(f"POT Provider: 预热未成功 — {msg}")
                        break
                    elif attempt < 2:
                        logger.debug(f"POT Provider 启动尝试 {attempt + 1} 失败，1秒后重试...")
                        time.sleep(1)
                    else:
                        logger.warning("POT Provider 服务启动失败：已达到最大重试次数")
            except Exception as e:
                try:
                    from loguru import logger

                    logger.warning(f"POT Provider 服务启动异常: {e}")
                except Exception:
                    pass
            finally:
                self.finished_signal.emit()

    class PotSplashBox(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

            self.setStyleSheet("background-color: transparent;")

            from qfluentwidgets import IndeterminateProgressRing, SubtitleLabel, isDarkTheme

            self.panel = QWidget(self)
            self.panel.setObjectName("PotSplashPanel")
            bg_color = "rgba(32, 32, 32, 0.95)" if isDarkTheme() else "rgba(255, 255, 255, 0.95)"
            text_color = "white" if isDarkTheme() else "black"

            self.panel.setStyleSheet(f"""
                QWidget#PotSplashPanel {{
                    background-color: {bg_color};
                    border-radius: 12px;
                    border: 1px solid rgba(128, 128, 128, 0.2);
                }}
                QLabel {{
                    color: {text_color};
                }}
            """)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(10, 10, 10, 10)
            layout.addWidget(self.panel)

            panel_layout = QVBoxLayout(self.panel)
            panel_layout.setContentsMargins(30, 30, 30, 30)
            panel_layout.setSpacing(15)

            self.ring = IndeterminateProgressRing(self.panel)
            self.ring.setFixedSize(50, 50)
            panel_layout.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignHCenter)

            self.lbl = SubtitleLabel("正在预热 YouTube 验证引擎 ...", self.panel)
            panel_layout.addWidget(self.lbl, 0, Qt.AlignmentFlag.AlignHCenter)

            self.ring.start()
            self.setFixedSize(350, 200)

    def launch_main_window():
        from fluentytdl.ui.reimagined_main_window import MainWindow

        # 恢复应用退出机制
        app.setQuitOnLastWindowClosed(True)
        window = MainWindow()
        window.show()
        app._main_window = window  # 保持引用防回收

        # === Cookie Sentinel: 启动时静默预提取 (Best-Effort) ===
        def start_cookie_sentinel_thread():
            import time

            time.sleep(2)  # 延迟 2 秒，不阻塞主界面
            try:
                from fluentytdl.auth.cookie_sentinel import cookie_sentinel

                cookie_sentinel.silent_refresh_on_startup()
            except Exception as e:
                try:
                    from loguru import logger

                    logger.debug(f"Cookie Sentinel 启动失败（预期行为）: {e}")
                except Exception:
                    pass

        import threading

        cookie_thread = threading.Thread(
            target=start_cookie_sentinel_thread, daemon=True, name="CookieSentinel-Startup"
        )
        cookie_thread.start()

    # 启动控制
    if config_manager.get("pot_provider_enabled", True):
        # 预热期间阻止最后窗口关闭退出程序
        app.setQuitOnLastWindowClosed(False)

        splash = PotSplashBox()
        desktop = QApplication.screens()[0].availableGeometry()
        w, h = desktop.width(), desktop.height()
        splash.move(w // 2 - splash.width() // 2, h // 2 - splash.height() // 2)
        splash.show()

        app._splash = splash  # 防回收

        init_thread = PotInitThread()
        app._init_thread = init_thread

        # 包装器避免多次启动
        is_launched = [False]

        def on_pot_ready():
            if is_launched[0]:
                return
            is_launched[0] = True
            try:
                splash.close()
                splash.deleteLater()
            except Exception:
                pass
            launch_main_window()

        init_thread.finished_signal.connect(on_pot_ready)
        # 最多等待 12 秒
        QTimer.singleShot(12000, on_pot_ready)
        init_thread.start()
    else:
        # 直接启动
        launch_main_window()

    # 4. 进入事件循环
    exit_code = app.exec()

    # === 5. 停止 POT Provider 服务 ===
    try:
        from fluentytdl.youtube.pot_manager import pot_manager

        pot_manager.stop_server()
    except Exception:
        pass

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
