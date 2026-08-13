"""
FluentYTDL 启动信息日志

每次应用启动时记录软件版本、Python/Qt 版本、安装类型和所有组件版本。
便于排查问题时快速了解运行环境。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def detect_install_type() -> str:
    """检测安装类型: setup (Program Files) / full (便携) / dev (开发)。"""
    if not getattr(sys, "frozen", False):
        return "dev"

    exe_path = Path(sys.executable).resolve()
    exe_str = str(exe_path).lower()

    program_files = Path.home().parent  # fallback
    import os

    program_files = os.environ.get("ProgramFiles", "C:\\Program Files").lower()
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)").lower()

    if exe_str.startswith(program_files) or exe_str.startswith(program_files_x86):
        return "setup"
    return "full"


def _quick_detect_version(key: str, exe_path: Path) -> str:
    """快速检测组件版本，3 秒超时避免阻塞启动。"""
    if not exe_path.exists():
        return "未安装"

    import re

    cmd_map = {
        "yt-dlp": [str(exe_path), "--version"],
        "ffmpeg": [str(exe_path), "-version"],
        "deno": [str(exe_path), "--version"],
        "pot-provider": [str(exe_path), "--version"],
        "atomicparsley": [str(exe_path), "--version"],
    }

    cmd = cmd_map.get(key, [str(exe_path), "--version"])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        output = (result.stdout + result.stderr).strip()
        if not output:
            return "已安装 (无版本输出)"

        first_line = output.split("\n")[0].strip()

        # 各工具版本解析
        if key == "yt-dlp":
            # yt-dlp 直接输出版本号如 "2025.11.12"
            return first_line
        elif key == "ffmpeg":
            # ffmpeg version n7.1.3-40-gcddd06f3b9-20260219
            m = re.search(r"ffmpeg version ([^\s]+)", first_line)
            if m:
                raw = m.group(1).lstrip("nN")
                vm = re.match(r"(\d+(?:\.\d+)*)", raw)
                return vm.group(1) if vm else raw
        elif key == "deno":
            # deno 1.38.0 (release, x86_64-pc-windows-msvc)
            m = re.search(r"deno (\d+\.\d+\.\d+)", first_line)
            if m:
                return m.group(1)
        elif key == "pot-provider":
            m = re.search(r"(\d+\.\d+\.\d+)", first_line)
            if m:
                return m.group(1)
        elif key == "atomicparsley":
            m = re.search(r"(\d{8}\.\d{6})", first_line)
            if m:
                return m.group(1)

        return first_line[:40]  # 截断避免过长
    except subprocess.TimeoutExpired:
        return "超时"
    except Exception:
        return "检测失败"


def log_startup_info() -> tuple[list[str], list[str]]:
    """记录启动版本信息到日志，并把该弹给用户看的迁移问题交回调用方。

    返回 ``(failures, conflicts)``。**为什么由这个函数带出去**：
    `paths.take_migration_report()` 是取走即清空的语义，谁先调谁就是唯一的
    消费者；而本函数在 `launch_main_window()` 里比任何 UI 提示都先跑，
    main.py 再取一次只会拿到两个空列表。冲突时静默选一份数据是不可接受的，
    所以带出来的这两个列表就是 InfoBar 的唯一数据源。
    """
    from fluentytdl.utils.logger import logger

    try:
        from fluentytdl import __version__
    except ImportError:
        __version__ = "unknown"

    try:
        import PySide6

        qt_version = PySide6.QtCore.qVersion()
        pyside_version = PySide6.__version__
    except Exception:
        qt_version = "unknown"
        pyside_version = "unknown"

    install_type = detect_install_type()
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()

    logger.info("=" * 50)
    logger.info(f"  FluentYTDL {__version__} 启动")
    logger.info(f"  Python {sys.version.split()[0]} | PySide6 {pyside_version} | Qt {qt_version}")
    logger.info(f"  安装类型: {install_type} | 路径: {app_dir}")
    logger.info("-" * 50)

    migration_failures, migration_conflicts = _replay_migration_report()

    # 组件版本检测
    from fluentytdl.utils.paths import frozen_app_dir as _frozen_app_dir

    if getattr(sys, "frozen", False):
        base = _frozen_app_dir() / "bin"
    else:
        base = app_dir / "assets" / "bin"

    components = [
        ("yt-dlp", "yt-dlp/yt-dlp.exe"),
        ("ffmpeg", "ffmpeg/ffmpeg.exe"),
        ("deno", "deno/deno.exe"),
        ("pot-provider", "pot-provider/bgutil-pot-provider.exe"),
        ("atomicparsley", "atomicparsley/AtomicParsley.exe"),
    ]

    for key, rel_path in components:
        exe_path = base / rel_path
        version = _quick_detect_version(key, exe_path)
        logger.info(f"  {key:<16} {version}")

    logger.info("=" * 50)

    return migration_failures, migration_conflicts


def _replay_migration_report() -> tuple[list[str], list[str]]:
    """把 `paths.py` 攒下的数据迁移消息回放到 loguru，返回 ``(failures, conflicts)``。

    为什么要"攒"再"放"：迁移跑在 `main.py` 的单实例锁之后，比 loguru 的落点确定
    还早；而 `paths.py` **不能 import loguru** —— `utils/logger.py:13` 在导入期就
    调 `user_data_dir()`，反向 import 会成环。所以 paths 只往模块级列表里塞字符串，
    由这里（主窗口已出现、日志已就位）取走。零新增 hook。

    **不清 `_MIGRATION_OK`**：`commit_migration_marker()` 还要靠它判断"零失败"，
    而本函数比 `finalize_startup()` 先跑 —— 详见 `paths.take_migration_report()`。
    """
    from fluentytdl.utils.logger import logger

    try:
        from fluentytdl.utils.paths import take_migration_report

        log, failures, conflicts = take_migration_report()
    except Exception as e:
        logger.debug(f"读取数据迁移报告失败: {e}")
        return [], []

    if not log and not failures and not conflicts:
        return [], []

    logger.info("  数据迁移报告:")
    for line in log:
        logger.info(f"    {line}")
    for line in failures:
        logger.warning(f"    迁移失败: {line}")
    for line in conflicts:
        logger.warning(f"    迁移冲突: {line}")
    logger.info("-" * 50)

    return failures, conflicts
