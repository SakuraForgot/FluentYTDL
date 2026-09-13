"""
FluentYTDL 启动信息日志

每次应用启动时记录软件版本、Python/Qt 版本、安装类型和所有组件版本。
便于排查问题时快速了解运行环境。
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.message_catalog import english

# 启动日志里要报版本的外部组件。顺序决定日志里的行序。
COMPONENTS: tuple[tuple[str, str], ...] = (
    ("yt-dlp", "yt-dlp/yt-dlp.exe"),
    ("ffmpeg", "ffmpeg/ffmpeg.exe"),
    ("deno", "deno/deno.exe"),
    ("pot-provider", "pot-provider/bgutil-pot-provider.exe"),
    ("atomicparsley", "atomicparsley/AtomicParsley.exe"),
)


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


def _resolve_component(key: str, base: Path, rel_path: str) -> tuple[Path | None, str]:
    """按"自带目录 → 系统 PATH"的顺序解析组件，返回 ``(路径, 来源)``。

    来源为 ``"内置"`` / ``"环境 PATH"``；两处都没有时返回 ``(None, "")``。

    **为什么启动日志也得看 PATH**：真正执行工具的 `locate_runtime_tool()` 是
    "自带 → PATH → 报错"三段式。只看自带目录的话，把 deno/ffmpeg 装在 PATH 上的
    用户会在启动日志里看到一排「未安装」，而 yt-dlp 全程用得很好 —— 排查问题时
    这种假线索比没有日志更糟。
    """
    if key == "yt-dlp":
        from fluentytdl.youtube.yt_dlp_cli import resolve_yt_dlp_runtime

        runtime = resolve_yt_dlp_runtime()
        return runtime.path, runtime.source

    bundled = base / rel_path
    if bundled.exists():
        return bundled, english("内置")

    import shutil

    try:
        from fluentytdl.core.dependency_manager import PATH_EXE_ALIASES

        aliases = PATH_EXE_ALIASES.get(key, (Path(rel_path).stem,))
    except Exception:
        aliases = (Path(rel_path).stem,)

    for name in aliases:
        found = shutil.which(name)
        if found:
            return Path(found), english("环境 PATH")

    return None, ""


def _quick_detect_version(key: str, exe_path: Path) -> str:
    """快速检测组件版本，3 秒超时避免阻塞启动。"""
    if key == "yt-dlp":
        from .ytdlp_runtime import probe_version

        result = probe_version(exe_path)
        return f"{result.version} ({result.channel or result.status})"

    if not exe_path.exists():
        return english("未安装")

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
            return english("已安装 (无版本输出)")

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
        return english("超时")
    except Exception:
        return english("检测失败")


def log_startup_info() -> tuple[list[str], list[str]]:
    """记录启动版本信息到日志，并把该弹给用户看的迁移问题交回调用方。

    返回 ``(failures, conflicts)``。**为什么由这个函数带出去**：
    `paths.take_migration_report()` 是取走即清空的语义，谁先调谁就是唯一的
    消费者；而本函数在 `launch_main_window()` 里比任何 UI 提示都先跑，
    main.py 再取一次只会拿到两个空列表。冲突时静默选一份数据是不可接受的，
    所以带出来的这两个列表就是 InfoBar 的唯一数据源。

    **本函数必须保持廉价。** 它由 `launch_main_window()` 在 `window.show()` 之后、
    `app.exec()` 之前同步调用，所以它花掉的时间全部落在「窗口已可见但点不动」那段。
    外部组件的版本表因此挪进了后台线程（`_spawn_component_version_probe()`）；
    这里剩下的只有内存里现成的信息和一条配置快照。往里加东西之前先量一下耗时。
    """
    from fluentytdl.utils.logger import logger

    _install_observability_sinks()

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
    log_text(logger, "info", "  FluentYTDL {0} 启动", __version__)
    logger.info(f"  Python {sys.version.split()[0]} | PySide6 {pyside_version} | Qt {qt_version}")
    log_text(logger, "info", "  安装类型: {0} | 路径: {1}", install_type, app_dir)
    logger.info("-" * 50)

    migration_failures, migration_conflicts = _replay_migration_report()

    _log_config_snapshot()

    logger.info("=" * 50)

    # 组件版本检测挪到后台线程 —— 详见 `_spawn_component_version_probe()` 的注释。
    _spawn_component_version_probe()

    return migration_failures, migration_conflicts


def _component_base_dir() -> Path:
    """自带组件的根目录（frozen 用 `bin/`，开发用 `assets/bin/`）。"""
    from fluentytdl.utils.paths import frozen_app_dir as _frozen_app_dir
    from fluentytdl.utils.paths import project_root as _project_root

    if getattr(sys, "frozen", False):
        return _frozen_app_dir() / "bin"
    # 开发模式必须用 project_root()，不能用 Path.cwd()：
    # 从别的目录起 python main.py 时 cwd 不是仓库根，整张组件表会全报「未安装」。
    return _project_root() / "assets" / "bin"


def log_component_versions() -> None:
    """把外部组件的版本表落进日志。**同步**，会跑 5 个子进程。

    正常启动路径不直接调这里，走 `_spawn_component_version_probe()`。
    """
    from fluentytdl.utils.logger import logger

    base = _component_base_dir()
    lines: list[str] = []
    versions = {}

    for key, rel_path in COMPONENTS:
        exe_path, source = _resolve_component(key, base, rel_path)
        if exe_path is None:
            lines.append(english("  {0:<16} 未安装", key))
            continue
        version = _quick_detect_version(key, exe_path)
        versions[key] = {"version": version, "source": source, "path": str(exe_path)}
        if key == "yt-dlp":
            from .ytdlp_runtime import probe_version

            identity = probe_version(exe_path)
            versions[key].update(
                version=identity.version, channel=identity.channel, status=identity.status
            )
        lines.append(f"  {key:<16} {version:<24} [{source}] {exe_path}")

    from fluentytdl.observability import emit_event

    emit_event("config", scope="toolchain", versions=versions)

    # 攒齐再一次性输出：这张表是排查工单时要整块读的，中间夹进别的线程写的行会打散它。
    logger.info("-" * 50)
    log_text(logger, "info", "  组件版本:")
    for line in lines:
        logger.info(line)
    logger.info("-" * 50)


def _spawn_component_version_probe() -> None:
    """把组件版本探测扔进后台线程。

    **为什么必须离开主线程**：`log_startup_info()` 由 `main.py::launch_main_window()`
    在 `window.show()` **之后**、`app.exec()` **之前**同步调用，所以它花的每一毫秒都落在
    「窗口已经画出来了，但事件循环还没开始转」这段里 —— 用户看得见界面、点不动界面，
    鼠标一直转圈。而这张表要跑 5 个 `--version` 子进程，其中 `yt-dlp.exe` 是
    PyInstaller onefile，每次启动都要把自己解压到 %TEMP% 再执行，实测约 1.4 秒
    （冷启动 + Defender 实时扫描会更久），单它一个就占掉整段冻结的九成。

    探测结果**没有任何消费者**，纯粹是给排查工单用的日志，所以离开主线程零代价：
    没人在等它的返回值，落进日志晚一两秒也仍然在日志开头那几屏里。

    daemon 线程：探测还没跑完用户就退出的话，直接跟着进程走，不拖住 teardown。

    整段裹 try/except：启动日志再有价值也不该拦住启动（硬规则 5）。
    """
    from fluentytdl.utils.logger import logger

    def run() -> None:
        try:
            log_component_versions()
        except Exception as exc:  # noqa: BLE001 - 硬规则 5
            log_text(logger, "debug", "组件版本探测失败: {0}", exc)

    try:
        threading.Thread(target=run, name="StartupInfo-Components", daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - 硬规则 5
        log_text(logger, "debug", "启动组件版本探测线程失败: {0}", exc)


def _install_observability_sinks() -> None:
    """接上 JSONL trace sink 与同步 ERROR sink，并清扫过期 trace。

    **顺序是有讲究的**：先清扫再装 sink。反过来的话清扫会去碰 JSONL sink 刚打开的
    句柄 —— Windows 上那是一个删不掉的文件，清扫每次启动都会在同一处失败。

    **为什么在 `log_startup_info()` 的最前面**：本函数下面那条配置快照就是第一条
    Observability 事件，sink 没装好它就只进文本日志、不进 JSONL。而 JSONL 里没有
    "这次启动的生效配置"，bug 包就少了排查任何工单的第一份材料。

    同步 ERROR sink（`enqueue=False`）的意义只在崩溃那一刻：主文件 sink 是
    `enqueue=True`，进程被强杀时队列里没写出去的那几行一起消失，而最后一条 ERROR
    恰恰是最想看的那一行。

    整段裹 try/except：日志系统装不上也不该拦住启动（硬规则 5）。
    """
    from fluentytdl.utils.logger import logger

    try:
        from fluentytdl.observability import install_sinks

        install_sinks()
    except Exception as exc:  # noqa: BLE001 - 硬规则 5
        log_text(logger, "debug", "接入 Observability sink 失败: {0}", exc)


def _log_config_snapshot() -> None:
    """把影响下载行为的生效配置落成一条 `kind=config scope=snapshot`。

    **为什么挂在这里**：这个函数已经是"每次启动记一遍运行环境"的落点（版本、工具链、
    安装类型），而生效配置和它们是同一类东西 —— 排查任何工单的第一步。放进同一个块里，
    用户贴上来的日志开头就能一次读完全部前提。

    **为什么函数级 import**：`utils` 是 Foundation 层，`core` 在它上面，模块级
    import `core.config_manager` 是反向依赖。上面的 `_resolve_component()` 取
    `PATH_EXE_ALIASES` 用的也是同一个办法。

    整段裹 try/except：启动日志再有价值也不该拦住启动（硬规则 5）。
    """
    try:
        from fluentytdl.core.config_manager import config_manager
        from fluentytdl.observability.config_snapshot import emit_config_snapshot

        emit_config_snapshot(config_manager.get)
        import hashlib
        import json

        from fluentytdl import __version__
        from fluentytdl.diagnostics.rules import get_rule_set
        from fluentytdl.observability import emit_event
        from fluentytdl.utils.log_runtime import SESSION_ID
        from fluentytdl.utils.paths import frozen_app_dir, is_frozen, project_root

        build = {"available": False, "mode": "frozen" if is_frozen() else "source"}
        build_path = (frozen_app_dir() if is_frozen() else project_root()) / "BUILD_INFO.json"
        try:
            if build_path.is_file() and build_path.stat().st_size < 1024 * 1024:
                data = json.loads(build_path.read_text(encoding="utf-8"))
                build = {
                    key: data.get(key)
                    for key in ("app_version", "git_commit", "dirty", "built_at_utc", "release_tag")
                }
                build["available"] = True
        except (OSError, ValueError):
            pass
        emit_event(
            "config",
            scope="runtime",
            snapshot_id=SESSION_ID,
            app_version=__version__,
            python=sys.version.split()[0],
            install_type=detect_install_type(),
            rules_version=get_rule_set().version,
            rules_hash=hashlib.sha256(repr(get_rule_set().rules).encode()).hexdigest(),
            build=build,
        )
    except Exception as exc:  # noqa: BLE001 - 硬规则 5
        from fluentytdl.utils.logger import logger

        log_text(logger, "debug", "记录生效配置快照失败: {0}", exc)


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
        log_text(logger, "debug", "读取数据迁移报告失败: {0}", e)
        return [], []

    if not log and not failures and not conflicts:
        return [], []

    log_text(logger, "info", "  数据迁移报告:")
    for line in log:
        logger.info(f"    {line}")
    for line in failures:
        log_text(logger, "warning", "    迁移失败: {0}", line)
    for line in conflicts:
        log_text(logger, "warning", "    迁移冲突: {0}", line)
    logger.info("-" * 50)

    return failures, conflicts
