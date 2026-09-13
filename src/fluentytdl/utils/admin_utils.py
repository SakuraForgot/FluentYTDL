"""
管理员权限工具
提供检查、请求、重启等管理员权限相关功能
"""

import ctypes
import os
import sys

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.message_catalog import standalone_text


def is_admin() -> bool:
    """
    检查当前进程是否以管理员身份运行

    Returns:
        bool: True 表示当前是管理员权限
    """
    if sys.platform != "win32":
        # Unix-like: 检查是否为 root
        return os.geteuid() == 0

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def restart_as_admin(reason: str = "", auto_restart: bool = False) -> bool:
    """
    以管理员身份重启当前程序

    Args:
        reason: 需要管理员权限的原因（显示给用户）
        auto_restart: 是否自动重启（不弹确认框）

    Returns:
        bool: True 表示成功请求重启，False 表示用户取消或失败
    """
    if sys.platform != "win32":
        from ..utils.logger import logger

        log_text(logger, "warning", "非 Windows 系统不支持自动提权重启")
        return False

    if is_admin():
        from ..utils.logger import logger

        log_text(logger, "info", "已经是管理员权限，无需重启")
        return False

    try:
        from ..utils.logger import logger

        log_text(logger, "info", "请求以管理员身份重启: {0}", reason)

        # 获取当前可执行文件路径
        if getattr(sys, "frozen", False):
            # 打包后的 exe
            exe_path = sys.executable
        else:
            # 开发环境：重启 Python 解释器
            exe_path = sys.executable
            # 添加脚本参数
            params = f'"{sys.argv[0]}"'
            if len(sys.argv) > 1:
                params += " " + " ".join(f'"{arg}"' for arg in sys.argv[1:])

        # 添加 --admin-mode 标记，表示这是管理员重启
        params = "--admin-mode"

        # 使用 ShellExecuteW 请求提权
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,  # hwnd
            "runas",  # lpOperation (请求管理员权限)
            exe_path,  # lpFile
            params,  # lpParameters
            None,  # lpDirectory
            1,  # nShowCmd (SW_SHOWNORMAL)
        )

        if ret > 32:  # 成功
            log_text(logger, "info", "已请求管理员重启，当前进程即将退出")
            # 给新进程一点启动时间
            import time

            time.sleep(0.5)
            # 退出当前进程
            sys.exit(0)
        else:
            log_text(logger, "warning", "用户取消了管理员权限请求 (返回码: {0})", ret)
            return False

    except Exception:
        from ..utils.logger import logger

        log_text(logger, "exception", "请求管理员重启失败")
        return False


def get_admin_status_message() -> str:
    """
    获取当前管理员状态的友好消息

    Returns:
        str: 状态消息
    """
    if is_admin():
        return standalone_text("✅ 当前以管理员身份运行")
    else:
        return standalone_text("ℹ️ 当前以普通用户身份运行")


def should_run_as_admin() -> bool:
    """
    判断程序是否应该以管理员身份运行

    检查配置或用户偏好，决定是否需要管理员权限

    Returns:
        bool: True 表示建议以管理员身份运行
    """
    # 可以从配置读取用户的偏好设置
    # 或者根据上次的 Cookie 提取失败情况判断
    return False  # 默认不强制
