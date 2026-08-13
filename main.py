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


def _extract_and_remove_arg(flag: str) -> str:
    """取出 `--flag VALUE` / `--flag=VALUE` 的值，并把它从 sys.argv 摘掉。

    两种形态都要支持：updater 侧用 `CreateProcessWithTokenW` 传的是一整条命令行
    字符串，带空格的路径必须整体加引号，分开写和等号写法都可能出现。

    摘掉是必须的 —— 未识别的参数会被 Qt 当成待打开的文件/URL。
    """
    prefix = flag + "="
    value = ""
    kept: list[str] = []
    i = 0
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == flag:
            if i + 1 < len(sys.argv):
                value = sys.argv[i + 1]
                i += 2
            else:
                i += 1  # 尾部悬空的 flag，直接丢弃
            continue
        if arg.startswith(prefix):
            value = arg[len(prefix) :]
            i += 1
            continue
        kept.append(arg)
        i += 1
    sys.argv = kept
    return value


# === 更新握手参数（由 updater 降权启动新版本时传入，见 core/updater.py 的 Step 6）===
#
# **这是一份跨模块的隐式契约，两端都在注释里点名对方**，别把其中一半当死代码删掉：
#   FLUENTYTDL_DATA_DIR_OVERRIDE   ← 这里写；utils/paths.py::user_data_dir() 第一优先级读
#   FLUENTYTDL_UPDATE_READY_TOKEN  ← 这里写；utils/update_signal.py::finalize_startup() 读
#
# 为什么落在环境变量而不是模块级私有变量：此刻 src/ 还没进 sys.path（那发生在 main()
# 里），import 不到 paths；而即便 import 到了，main.py 的私有变量 paths.py 也看不见 ——
# 那是个一实施就会踩的鸡生蛋问题。os.environ 无依赖、不受 import 顺序影响，而且子进程
# 天然继承 —— 新版再拉起任何子进程都指向同一个数据目录，正是我们想要的。
_DATA_DIR_ARG = _extract_and_remove_arg("--data-dir")
if _DATA_DIR_ARG:
    os.environ["FLUENTYTDL_DATA_DIR_OVERRIDE"] = _DATA_DIR_ARG

_READY_TOKEN_ARG = _extract_and_remove_arg("--update-ready-token")
if _READY_TOKEN_ARG:
    os.environ["FLUENTYTDL_UPDATE_READY_TOKEN"] = _READY_TOKEN_ARG

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

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def _cleanup_update_residuals() -> None:
    """清理更新残留文件并恢复失败的更新。

    设计为 best-effort：任何失败都静默跳过，不阻塞启动。
    仅在 frozen（打包）模式下执行。

    处理场景：
    1. _internal_old/ 存在但 _internal/ 不完整 → 自动恢复旧版
    2. FluentYTDL.exe 不存在但 .exe.old 存在 → 自动恢复旧版
    3. _update_tmp/ 存在 → 清理失败的更新临时目录
    4. updater.exe.new 存在 → 延迟替换 updater.exe
    5. 常规清理：updater.exe.old、%TEMP% 临时目录

    注意场景 1/2 只在**明确的半残状态**下才动手。完好的 `_internal_old/` 与
    `FluentYTDL.exe.old` 是 updater 看门狗的回滚素材，本函数不碰 —— 详见 §4 注释。
    """
    if not getattr(sys, "frozen", False):
        return

    import shutil
    import tempfile

    app_dir = Path(sys.executable).resolve().parent
    exe_name = Path(sys.executable).name
    exe_path = app_dir / exe_name
    internal_dir = app_dir / "_internal"
    internal_old = app_dir / "_internal_old"
    exe_old = exe_path.with_suffix(".exe.old")
    tmp_update_dir = app_dir / "_update_tmp"
    updater_new = app_dir / "updater.exe.new"
    updater_path = app_dir / "updater.exe"
    updater_old = app_dir / "updater.exe.old"

    # === 1. 恢复失败的更新 ===

    # 场景 A: _internal_old 存在且 _internal 不存在或为空 → 恢复
    if internal_old.exists():
        need_restore = False
        if not internal_dir.exists():
            need_restore = True
        elif not any(internal_dir.iterdir()):
            need_restore = True

        if need_restore:
            try:
                if internal_dir.exists():
                    shutil.rmtree(internal_dir, ignore_errors=True)
                internal_old.rename(internal_dir)
            except Exception:
                pass

    # 场景 B: 主 exe 不存在但 .exe.old 存在 → 恢复
    if exe_old.exists() and not exe_path.exists():
        try:
            exe_old.rename(exe_path)
        except Exception:
            pass

    # === 2. 清理失败的更新临时目录 ===
    if tmp_update_dir.exists():
        try:
            shutil.rmtree(tmp_update_dir, ignore_errors=True)
        except Exception:
            pass

    # === 3. 延迟替换 updater.exe ===
    #
    # `updater.exe` 不在 app-core 归档里（用户机器上它正在运行，覆写不了），所以
    # 修在 updater 里的东西没法直接送到已安装用户手上 —— 那会是个死锁。投递靠
    # `updater.exe.new`：对旧 updater 它只是归档里一个普通文件，搬进安装目录即完成
    # 投递，替换则由两条路径之一完成：
    #   · 便携版 / 可写安装路径 → 这里（普通权限就够）
    #   · Program Files        → 提权 updater 退出后的 helper
    #     （core/updater.py::_self_update_updater，同一次更新的末尾）
    # 两条互为兜底：helper 那次没跑成（比如这次更新是旧 updater 做的，它没有 Step 8），
    # 下次启动由这里补上；这里因权限失败，则由下次更新的新 updater 补上。
    if updater_new.exists():
        # 先判"要不要换"。full.7z 与 setup.exe 都会带上 `.new`（它们本来就带
        # updater.exe，两者由 build_updater() 同一份产物 copy2 而来），所以全新安装
        # 时这两个文件的 size 与 mtime 必然相等 —— 那不是一次待办的替换，只是打包
        # 副产物。不加这道判断的话，安装版用户每次启动都会对着 Program Files 里
        # 一个根本不需要动的文件试一次注定失败的替换。
        try:
            new_stat = updater_new.stat()
            cur_stat = updater_path.stat() if updater_path.exists() else None
            same_build = (
                cur_stat is not None
                and new_stat.st_size == cur_stat.st_size
                and new_stat.st_mtime == cur_stat.st_mtime
            )
        except OSError:
            same_build = False

        if not same_build:
            # **单条件判断**：不能再要求 `updater_path.exists()`。3.6.6 之前的
            # updater 会在收尾时 self_delete 把自己删掉，于是这个 and 条件恒为假，
            # `.new` 永远躺着不生效 —— 这正是"所有修复都送不出去"的另一半原因。
            try:
                os.replace(updater_new, updater_path)
            except OSError as e:
                # **保留 `.new`，不要删。** 删了就等下一次更新才有机会重试，而
                # Program Files 用户恰恰是最需要这次替换的那批（他们的 updater 带着
                # UAC 递归 bug）。留着，下次启动或下次更新再试。
                try:
                    from loguru import logger

                    logger.warning(
                        f"[Startup] updater.exe 替换失败，已保留 updater.exe.new 待下次重试: {e}"
                    )
                except Exception:
                    # 此刻 loguru 的落点还没配置（logger 依赖 paths，而 paths 依赖
                    # 数据目录推导），frozen 无窗口模式下这行可能哪儿都不去。
                    # 权威记录在 updater 自己的 logs/updater.log 里。
                    pass

    # === 4. 常规清理 ===
    #
    # **这里故意不再清理 `_internal_old/` 与 `FluentYTDL.exe.old`。**
    #
    # 从 3.6.6 起 updater 启动新版之后不退出，而是留下来当看门狗：收到 READY 信号才
    # COMMIT（删备份），新版起不来就 ROLLBACK（拿备份还原）—— 见 core/updater.py 的
    # Step 7。此刻正在跑的这个进程**就是被监护的那一个**。在这里删备份，等于在裁判
    # 判决之前销毁唯一的回滚素材：随后崩一次就永远回不去了。备份的清理权归 updater。
    #
    # 场景 A/B（上面的恢复逻辑）保留：那是 updater 被强杀（任务管理器、断电、蓝屏）
    # 之后的兜底，触发条件是"_internal 缺失或为空 / exe 不存在"这种明确的半残状态，
    # 与看门狗正常工作时"备份在、_internal 也完好"的形态区分得开。
    if updater_old.exists():
        # 唯一例外：updater 自更新 helper 留下的 `updater.exe.old`。
        # 它不是任何东西的回滚素材（helper 只在 COMMIT 之后才跑），helper 自己
        # 的 `del` 可能因时序失败，所以在这里兜一次。绝不扩大成 `*.exe.old` 通配。
        try:
            updater_old.unlink(missing_ok=True)
        except Exception:
            pass

    # 清理 %TEMP% 中过时的更新临时目录
    try:
        temp_root = Path(tempfile.gettempdir())
        for tmp_dir in temp_root.glob("fluentytdl_update_*"):
            if tmp_dir.is_dir():
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass


def _show_migration_infobars(window, failures: list[str], conflicts: list[str]) -> None:
    """把数据迁移的冲突 / 失败弹给用户看。全程 best-effort，绝不影响启动。

    **为什么必须弹**：迁移遇到两处都有真实数据时会挑较新的一份装进数据目录，落选
    的那份被搬到 `legacy_conflict_<tag>/`。**静默选一个是不可接受的** —— 从用户视角
    看就是"更新之后我的设置/任务变回了旧的样子"，而真正想要的那份就躺在旁边的子目录
    里没人告诉他。日志里已经有完整清单（`utils/startup_info.py::_replay_migration_report`），
    但没人会主动去翻日志。

    冲突条用 `duration=-1` 常驻：里面有用户需要照着去找的路径，被 5 秒动画卷走等于没说。
    失败条会在下次启动自动重试（`.migrated_v2` 只在零失败时才写），给个有限时长就够。
    """
    try:
        from qfluentwidgets import InfoBar, InfoBarPosition

        if conflicts:
            detail = "\n".join(conflicts[:3])
            if len(conflicts) > 3:
                detail += f"\n……另有 {len(conflicts) - 3} 项，详见日志"
            InfoBar.warning(
                title="检测到两处旧数据",
                content="已保留较新的一份，另一份存放在数据目录的 legacy_conflict_* 子目录中：\n"
                + detail,
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=-1,
                parent=window,
            )

        if failures:
            InfoBar.warning(
                title="部分数据未能迁移",
                content=f"有 {len(failures)} 项迁移失败（通常是文件被占用），"
                "下次启动会自动重试。旧数据仍在原位置，未被删除。",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=20000,
                parent=window,
            )
    except Exception:
        pass


def main() -> None:
    # Ensure "src" is importable when running from repo root
    root_dir = Path(__file__).resolve().parent
    src_dir = root_dir / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    IS_UPDATE_WORKER = "--update-worker" in sys.argv
    if IS_UPDATE_WORKER:
        from fluentytdl.core.updater_worker import run_worker

        sys.exit(run_worker())

    # === 启动清理: 删除更新残留文件 ===
    _cleanup_update_residuals()

    # === 1. 修改缩放策略 (关键): 解决字体模糊问题 ===

    # 允许 Qt 使用操作系统的精确小数缩放比例 (如 125%, 150%)
    if hasattr(Qt.HighDpiScaleFactorRoundingPolicy, "PassThrough"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    # 显式声明 AppUserModelID：让 Windows 把窗口、任务栏图标和通知归到同一个应用身份下。
    # 不设置时 Windows 会按 EXE 路径隐式归组，覆盖安装后容易命中陈旧的图标缓存。
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FluentYTDL")
        except Exception:
            pass

    # 1. 创建应用
    app = QApplication(sys.argv)

    # === 单实例检测 ===
    from fluentytdl.utils.single_instance import SingleInstanceChecker

    single_instance = SingleInstanceChecker("FluentYTDL_SingleInstance_v1")
    if not single_instance.check_and_start():
        sys.exit(0)
    # 防回收
    app._single_instance = single_instance  # type: ignore[attr-defined]

    # === 数据迁移：把遗留位置的数据合并到 user_data_dir() ===
    #
    # **必须在单实例锁之后**：锁之前迁移会让两个实例同时搬 `tasks.db` 的 WAL 三件套。
    #
    # **必须在这一行之前**（顺序不是风格问题，是硬约束）：
    #   · utils/logger.py:13 的 `LOG_DIR = str(user_data_dir() / "logs")` 是**导入期
    #     求值的字符串常量**，被 ui/settings_page.py 和 log_viewer_dialog.py 按值
    #     import —— 一旦 logger 被导入，日志路径就冻结了；
    #   · core/config_manager.py 在 `_init` 里就调 `config_path()`；
    #   · core/i18n.py（下一行）会拖进 config_manager。
    # 此刻唯一被导入过的 fluentytdl 模块是 single_instance，它只碰 PySide6 +
    # qfluentwidgets，不碰 paths / logger / config_manager。
    #
    # 迁移只复制、绝不删源，且完成标记 `.migrated_v2` 不在这里写 —— 要等
    # finalize_startup() 确认本版本真的能跑（见 utils/update_signal.py）。
    # 报告由 utils/startup_info.py::log_startup_info() 回放到 loguru。
    try:
        from fluentytdl.utils.paths import migrate_user_data

        migrate_user_data()
    except Exception:
        # best-effort：迁移失败绝不阻塞启动。数据仍在旧位置未丢，日志会记录。
        pass

    def activate_main_window():
        main_win = getattr(app, "_main_window", None)
        if main_win:
            main_win.setWindowState(
                main_win.windowState() & ~Qt.WindowState.WindowMinimized
                | Qt.WindowState.WindowActive
            )
            main_win.show()
            main_win.raise_()
            main_win.activateWindow()

    single_instance.new_instance_detected.connect(activate_main_window)
    # === 初始化国际化 (i18n) ===
    from fluentytdl.core.i18n import I18nManager

    I18nManager.setup_language()

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

    # 应用图标：多尺寸 QIcon，供窗口 / 任务栏 / 托盘共用。
    # 必须走 resource_path()（它处理了 sys._MEIPASS），用 __file__ 推导的路径在打包后指不到资源。
    try:
        from fluentytdl.utils.icons import load_app_icon

        app_icon = load_app_icon()
        if not app_icon.isNull():
            app.setWindowIcon(app_icon)
    except Exception:
        pass

    # === 2. 设置全局字体 (关键) ===
    font = QFont("Microsoft YaHei UI", 9)
    try:
        # 在 100% 缩放下，必须启用完全的 Hinting (微调)，否则 DirectWrite 会导致字体发虚和模糊。
        # 绝对不能使用 PreferNoHinting，这只适合 150% 以上的高分屏或 Mac 系统。
        font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    except Exception:
        pass
    app.setFont(font)

    # Import UI after QApplication is created to avoid triggering Qt font operations
    # during module import (which can cause QFont warnings if done before app exists).
    # from fluentytdl.ui.main_window import MainWindow
    from fluentytdl.core.config_manager import config_manager

    def launch_main_window():
        from fluentytdl.core.controller import app_controller
        from fluentytdl.ui.reimagined_main_window import MainWindow

        # 恢复应用退出机制
        app.setQuitOnLastWindowClosed(True)
        # DI 注入 AppController
        window = MainWindow(app_controller)
        window.show()
        app._main_window = window  # type: ignore[attr-defined]  # 保持引用防回收

        # === 启动版本日志（顺带取回数据迁移报告）===
        #
        # log_startup_info() 是迁移报告的**唯一消费者** —— take_migration_report()
        # 取走即清空，谁先调谁就拿到全部。所以由它把该让用户看见的两类问题原样带回来，
        # 这里再弹 InfoBar；main.py 自己去 take 一次只会拿到两个空列表。
        migration_failures: list[str] = []
        migration_conflicts: list[str] = []
        try:
            from fluentytdl.utils.startup_info import log_startup_info

            migration_failures, migration_conflicts = log_startup_info()
        except Exception:
            pass

        if migration_failures or migration_conflicts:
            # 推一格到事件循环：InfoBar 是带动画的浮层，此刻 app.exec() 还没开始转。
            QTimer.singleShot(
                0,
                lambda: _show_migration_infobars(window, migration_failures, migration_conflicts),
            )

        # === 向 updater 看门狗宣告"本版本已就绪" ===
        #
        # 走到这里，关键服务已经全部就位：config_manager 已加载（上面 :241 就在用）、
        # task_db 已连接、download_manager 已恢复未完成任务、主窗口已 show()。
        #
        # 用 singleShot(0) 再推一格到事件循环里：window.show() 只证明 Qt 画出了窗口，
        # 而**事件循环真的转起来了**才是"这个版本能跑"的证明。updater 拿这个信号决定
        # COMMIT 还是 ROLLBACK（core/updater.py Step 7），所以宁可多等一格。
        from fluentytdl.utils.update_signal import finalize_startup

        QTimer.singleShot(0, finalize_startup)

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

    # 启动控制：主窗口立即出现，POT 预热挪到后台常驻线程（复用 cookie sentinel 范式）。
    # 旧实现用 PotSplashBox + 12s QTimer 兜底阻塞主窗口；POT 对解析速度没有正向贡献，
    # 它买的是"不被机器人检测拦"，因此没有任何理由让用户为它等待。
    if config_manager.get("pot_provider_enabled", False):
        try:
            from fluentytdl.youtube.pot_manager import pot_manager

            pot_manager.ensure_warm_async()
        except Exception as e:
            from loguru import logger

            logger.warning(f"POT 后台预热启动失败: {e}")

    launch_main_window()

    # 4. 进入事件循环
    exit_code = app.exec()

    # === 5. 停止 POT Provider 服务 ===
    try:
        from fluentytdl.youtube.pot_manager import pot_manager

        pot_manager.stop_server()
    except Exception:
        pass

    # === 6. 如果本次退出是为了应用更新，现在才启动 updater.exe ===
    # 必须是进程的最后一个动作：updater 一旦启动就在等这个进程的句柄 signaled，
    # 好开始替换安装目录里的文件。放在这里意味着 worker shutdown 与 db_writer
    # 落盘（可能耗时十几秒）已经全部完成 —— updater 只需覆盖解释器 teardown。
    # 非更新退出时状态是 IDLE，该函数直接返回，不做任何事。
    try:
        from fluentytdl.core.component_update_manager import component_update_manager

        component_update_manager.launch_pending_updater()
    except Exception as e:
        from loguru import logger

        logger.error(f"启动 updater 失败: {e}")

    sys.exit(exit_code)


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
