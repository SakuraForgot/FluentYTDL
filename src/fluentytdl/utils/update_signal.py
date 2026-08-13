"""更新握手信号：告诉正在监护本进程的 updater "这个版本能跑"。

配套 `core/updater.py` 的看门狗（Step 7）。整条链路：

    updater 生成 nonce → `--data-dir D --update-ready-token T` 启动新版
      → main.py 极早期把两者摘进环境变量（FLUENTYTDL_DATA_DIR_OVERRIDE /
        FLUENTYTDL_UPDATE_READY_TOKEN）
      → 关键服务全部就绪后 finalize_startup() 写 `D/.update_ready`
      → updater 读到 pid + token 双匹配 → COMMIT（删掉回滚素材）

**为什么不是 window.show() 就算成功**：show() 只证明 Qt 画出了窗口，此时
config 可能刚读到损坏内容、task_db 可能还没连上。READY 的语义是"关键服务
全部就绪"，所以调用点在 `launch_main_window()` 里、`show()` 之后，且经
`QTimer.singleShot(0, ...)` 让事件循环先转一圈 —— 转得动才算真的活着。

**为什么写文件而不是 named event**：跨提权级别的内核对象要显式
SECURITY_ATTRIBUTES 才能被降权进程 signal，`Global\\` 前缀还要
SeCreateGlobalPrivilege。文件轮询（updater 侧 0.5s）足够，而且看得见、可诊断。

纯标准库：本模块被最早期的启动路径调用，不能拖进 loguru / PySide6 依赖
（loguru 的落点本身就依赖 paths，见 utils/logger.py）。日志按需惰性导入。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

READY_FILENAME = ".update_ready"

# 超过这个年龄的 READY 文件一律视为陈旧残留（与 updater.py::STALE_READY_MAX_AGE 同值）。
# 真实握手的文件只会存在几秒 —— updater 收到就删。
STALE_READY_MAX_AGE = 300.0

TOKEN_ENV = "FLUENTYTDL_UPDATE_READY_TOKEN"
DATA_DIR_ENV = "FLUENTYTDL_DATA_DIR_OVERRIDE"


def _debug(msg: str) -> None:
    """尽力记一行日志，绝不因为日志失败影响启动。"""
    try:
        from loguru import logger

        logger.info(f"[UpdateSignal] {msg}")
    except Exception:
        pass


def _ready_dir() -> Path:
    """READY 文件的落点。

    **优先读环境变量而不是 user_data_dir()**：这个文件的位置由 updater 的
    `--data-dir` 单方面定义，updater 只在那一个路径下轮询。让它经过
    `user_data_dir()` 的推导逻辑，等于给一次一次性握手引入一条可能分歧的
    推导链 —— 而这条推导链正好是本轮要修的那条。
    """
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)

    from .paths import user_data_dir

    return user_data_dir()


def _write_ready_file(ready_path: Path, token: str) -> None:
    """原子写入：先落 .tmp 再 os.replace，避免 updater 读到半个 JSON。

    updater 侧对残缺内容是容错的（解析失败 → 当作"还没写好"继续等），
    但那是兜底，不是可以省掉原子写的理由 —— 半个 JSON 会让本次握手白等
    一个轮询周期，而 90s 预算里的每一秒都是用户在盯着一个空屏幕。
    """
    payload = {"pid": os.getpid(), "token": token, "ts": time.time()}
    tmp_path = ready_path.with_name(ready_path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp_path, ready_path)


def _cleanup_stale_ready(ready_path: Path) -> None:
    """删掉上一次会话遗留的过期 READY 文件（best-effort）。

    第一道防线在 updater 那边（启动新版前先删一次，它有管理员权限）。这里是
    第二道：万一那次删除失败，也不该留一个陈旧文件让下一轮更新读到。
    """
    try:
        if not ready_path.exists():
            return
        if time.time() - ready_path.stat().st_mtime < STALE_READY_MAX_AGE:
            return
        ready_path.unlink(missing_ok=True)
        _debug(f"已清理陈旧的 {READY_FILENAME}")
    except OSError:
        pass


def _commit_migration() -> None:
    """提交 Phase 6 的迁移标记。失败只记日志。

    与 READY 握手绑在同一个时刻，因为两者断言的是**同一个事实**：这个版本能跑。
    标记要是写早了，新版在 READY 之前崩溃被 updater 回滚之后，旧版会继续往旧
    路径写数据，而下次更新看到标记就跳过迁移、直接采用陈旧副本 —— 那是静默的
    数据丢失。

    与 token 无关，所以在 READY 分支**之外**无条件调用：正常双击启动同样需要
    提交上一次留下的迁移结果。`commit_migration_marker()` 自己会检查"本次启动
    真的跑过迁移且零失败"，没跑过就直接返回 False。
    """
    try:
        from .paths import commit_migration_marker

        if commit_migration_marker():
            _debug("已写出数据迁移完成标记")
    except Exception as e:
        _debug(f"提交迁移标记失败，下次启动将重试: {e}")


def finalize_startup() -> None:
    """宣告"本版本已就绪"。全程 best-effort，任何失败都不影响程序运行。

    调用时机见模块 docstring。做两件事，因为它们断言的是同一个事实——这个版本
    能跑起来：

    1. 写 `.update_ready`（只在 updater 传了 token 时；没有就跳过）
    2. 提交 Phase 6 的迁移标记 `.migrated_v2`（无条件尝试，见 `_commit_migration`）
    """
    try:
        ready_dir = _ready_dir()
        ready_path = ready_dir / READY_FILENAME
    except Exception as e:  # pragma: no cover - 路径推导失败属于环境异常
        _debug(f"无法确定 {READY_FILENAME} 落点，跳过: {e}")
        _commit_migration()
        return

    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        # 不是 updater 拉起的（正常双击启动 / 旧 updater 的 survival 模式）。
        # 没有握手对象，顺手做一次陈旧文件清理就够了。
        _cleanup_stale_ready(ready_path)
        _commit_migration()
        return

    try:
        ready_dir.mkdir(parents=True, exist_ok=True)
        _write_ready_file(ready_path, token)
        _debug(f"已写出 {READY_FILENAME} (pid={os.getpid()}) → {ready_path}")
    except OSError as e:
        # 写不进去 → updater 会在 90s 后判超时并回滚。回滚本身是安全的
        # （旧版会被重新启动），所以这里只记录，不抛。
        _debug(f"写出 {READY_FILENAME} 失败，updater 将按超时回滚: {e}")

    _commit_migration()
