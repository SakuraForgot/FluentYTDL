from __future__ import annotations

import sys
import threading

from loguru import logger

from .localized_log import install_metadata_sink
from .log_runtime import SafeFileSink, get_log_root, patch_record, record_failure

LOG_DIR = str(get_log_root())
_initialized = False


def initialize_logging() -> None:
    """Idempotent bootstrap; callers may explicitly initialize before services."""
    global _initialized
    if _initialized:
        return
    logger.remove()
    logger.configure(patcher=patch_record)
    console = getattr(sys, "__stderr__", None) or sys.stderr
    if console is not None:
        logger.add(
            console,
            level="INFO",
            diagnose=False,
            format=(
                "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
            ),
        )
    try:
        logger.add(
            SafeFileSink(get_log_root(), "app"),
            level="DEBUG",
            enqueue=True,
            backtrace=True,
            diagnose=False,
        )
    except Exception as exc:
        record_failure("app_install", exc)
    install_metadata_sink(logger, LOG_DIR)
    _initialized = True


initialize_logging()


# 5. 全局异常捕获钩子
# 这样即使程序崩溃（Crash），也能在日志里看到原因
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.opt(exception=(exc_type, exc_value, exc_traceback)).critical("Uncaught exception")


sys.excepthook = handle_exception


# 6. 线程里的未捕获异常
#
# `sys.excepthook` 只管主线程。子线程 `run()` 抛出去的东西走的是
# `threading.excepthook`（Python 3.8+），没装钩子时 CPython 只往 stderr 打一段
# traceback —— 而发布版是 windowed 进程，stderr 没有归宿，那段 traceback 直接消失。
# 项目里有 6 处裸线程（`executor.py` 的 section-part-progress、`yt_dlp_cli.py` 的
# `_cancel_watcher` 等），它们静默死掉的表现是"进度条永远不动"，日志里一个字都没有。
#
# **`ThreadPoolExecutor` 不在这个钩子的覆盖范围内** —— 池内异常被装进 `Future`，
# 工作线程本身是正常结束的，钩子永远不会触发。那一半由
# `observability/futures.py::observe_future()` 负责。
#
# 这里用裸 loguru 而不是 `emit_event`：`observability` 依赖本模块，反向 import 必成环。
def handle_thread_exception(args) -> None:
    if args.exc_type is None or issubclass(args.exc_type, SystemExit):
        return
    name = getattr(args.thread, "name", "?")
    logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).critical(
        "Uncaught exception in thread {}", name
    )


threading.excepthook = handle_thread_exception


# 导出 logger 供其他模块使用
def get_logger(*_args, **_kwargs):
    return logger
