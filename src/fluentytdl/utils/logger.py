from __future__ import annotations

import os
import sys
import tempfile
import threading

from loguru import logger

from .paths import is_frozen, user_data_dir

# 1. 确定日志存储路径
# 统一使用 user_data_dir()，开发模式在项目根目录，打包模式在 exe 同级目录
LOG_DIR = str(user_data_dir() / "logs")

if not os.path.exists(LOG_DIR):
    try:
        os.makedirs(LOG_DIR)
    except Exception:
        # 极端情况：无权限创建目录，降级为临时目录
        LOG_DIR = os.path.join(tempfile.gettempdir(), "FluentYTDL_logs")
        os.makedirs(LOG_DIR, exist_ok=True)


# 2. 重置 logger 配置
logger.remove()


# 3. 配置控制台输出 (开发调试用)
# level="INFO" 表示只显示 INFO, WARNING, ERROR, CRITICAL
_console_sink = getattr(sys, "__stderr__", None) or sys.stderr
if _console_sink is not None:
    logger.add(
        _console_sink,
        level="INFO",
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        ),
    )


# 4. 配置文件输出 (排查问题用)
# level="DEBUG" 记录所有细节
# rotation="00:00" 每天午夜轮转新文件
# retention="7 days" 只保留最近7天的日志
# compression="zip" 旧日志自动压缩
#
# `diagnose` 只在开发模式开：它会把异常帧里**每个局部变量的值**打进日志，而这条路上
# 的局部变量可能是 cookie 文件内容、PO Token、代理账号密码。发布版的日志是用户会直接
# 贴进 GitHub Issue 的东西，那等于把凭据贴上去。开发模式下没有这个暴露面，而变量值对
# 排查的价值又很高，所以按 `is_frozen()` 分开，而不是一刀切关掉。
# `backtrace` 保持开启：它只记调用栈，不记变量值。
_DIAGNOSE = not is_frozen()

logger.add(
    os.path.join(LOG_DIR, "app_{time:YYYY-MM-DD}.log"),
    level="DEBUG",
    rotation="00:00",
    retention="7 days",
    compression="zip",
    encoding="utf-8",
    enqueue=True,  # 异步写入，不阻塞主线程
    backtrace=True,  # 记录异常堆栈
    diagnose=_DIAGNOSE,  # 发布版关闭：见上
)


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
