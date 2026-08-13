from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 数据目录：常量与迁移报告队列
# ---------------------------------------------------------------------------

# 跨 Phase 的隐式契约：这个环境变量由 main.py 顶部（--data-dir 摘参处）写入，
# 只由本模块的 user_data_dir() 消费。updater 降权启动新版时通过 --data-dir 传进来，
# 让新版的数据目录完全不依赖"被继承的环境"。两边都别当死代码删掉。
DATA_DIR_ENV = "FLUENTYTDL_DATA_DIR_OVERRIDE"

#: 便携版标记。只注入 full.7z 归档根，绝不出现在 dist/ 与安装版里。
PORTABLE_MARKER = "portable.txt"

#: 迁移完成标记。**不由 migrate_user_data() 写** —— 等新版证明自己能跑之后，
#: 由 utils/update_signal.py::finalize_startup() 调 commit_migration_marker() 写出。
MIGRATION_MARKER = ".migrated_v2"

#: 留在每个遗留根里的面包屑，内容是新数据目录的路径。
MIGRATION_BREADCRUMB = ".migrated_to.txt"

#: 迁移暂存目录（位于目的地内部，保证后续 os.replace 同卷原子）。
MIGRATE_TMP_DIRNAME = ".migrate_tmp"

#: 参与迁移的项目，各自独立裁决。
MIGRATION_ITEMS = (
    "config.json",
    "state",
    "logs",
    "error_rules.override.json",
    "update_manifest_cache.json",
)

#: 落选后需要保留副本的项目（其余可再生，落选直接丢弃）。
_CONFLICT_KEPT_ITEMS = frozenset({"config.json", "state"})

#: 判定"同一份文件"的 mtime 容差（秒）。FAT / 网络盘的时间戳粒度可到 2 秒。
_MTIME_TOLERANCE = 2.0

# paths.py 处在导入图最底层，**不能 import loguru**
# （utils/logger.py:13 在导入期就调用 user_data_dir()，反向 import 会成环）。
# 迁移消息先攒在这里，由 utils/startup_info.py::log_startup_info() 回放到 loguru。
_MIGRATION_LOG: list[str] = []
_MIGRATION_FAILURES: list[str] = []
_MIGRATION_CONFLICTS: list[str] = []

# take_migration_report() 会清空上面三个列表，但**绝不能**清空这个标志位：
# log_startup_info() 比 finalize_startup() 先跑，若成败信息被一起取走，
# commit_migration_marker() 就会在有失败的情况下照样落 .migrated_v2，
# 正好绕开"零失败才写"这条规则。None 表示本次启动没跑过迁移。
_MIGRATION_OK: bool | None = None


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    # src/fluentytdl/utils/paths.py -> src/fluentytdl/utils -> src/fluentytdl -> src -> root
    return Path(__file__).resolve().parents[3]


def _local_app_data() -> Path:
    """%LOCALAPPDATA% 的等价物（非 Windows 上退回 XDG 风格路径）。"""
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if base:
        return Path(base)
    home = Path(os.path.expanduser("~"))
    if sys.platform == "win32":
        return home / "AppData" / "Local"
    return home / ".local" / "share"


def user_data_dir(app_name: str = "FluentYTDL") -> Path:
    """所有运行期数据（config / DB / logs）的根目录。

    优先级如下。**这里绝不做写权限探测** —— 旧实现用 `.writetest` 决定数据
    落在哪，于是提权会话（updater 曾让新版继承管理员令牌）写得进
    ``C:\\Program Files\\FluentYTDL``，普通会话写不进，同一台机器的数据
    分裂成两棵树，用户看到的就是"更新完设置和任务全没了"。

    ========================================  ====================================
    场景                                       位置
    ========================================  ====================================
    ``--data-dir`` / 环境变量覆盖               该路径（updater 降权启动新版时用）
    frozen 且 exe 同级有 ``portable.txt``       exe 同级目录（便携版 full.7z）
    frozen 且无标记                             ``%LOCALAPPDATA%\\<app_name>``
    非 frozen                                   ``project_root()``
    ========================================  ====================================

    行为变化：开发模式不再有 ``~/Documents`` 回退（那正是分裂的另一半）。
    """
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        target = Path(override)
    elif is_frozen():
        app_dir = frozen_app_dir()
        target = app_dir if (app_dir / PORTABLE_MARKER).exists() else _local_app_data() / app_name
    else:
        target = project_root()

    # 必需：youtube/yt_dlp_cli.py::_safe_working_dir() 拿 config_path().parent
    # 当 yt-dlp 子进程的 CWD，目录不存在子进程直接起不来。
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


def old_user_data_dir(app_name: str = "FluentYTDL") -> Path:
    """The legacy user data directory (~/Documents/FluentYTDL).
    Used only for one-time migration of existing data."""
    home = Path(os.path.expanduser("~"))
    return home / "Documents" / app_name


def _migrate_file(src: Path, dst: Path) -> bool:
    """One-time best-effort file migration. Returns True if migration happened.

    保留原样不动：`core/config_manager.py:217` 与 `storage/task_db.py:54,58` 三处
    仍在调用它。新的整体迁移走下面的 `migrate_user_data()`，两者互不干扰 ——
    这个函数只在目的地**不存在**时才动手，而 `migrate_user_data()` 跑在它们之前
    （main.py 单实例锁之后，config_manager / task_db 首次实例化之前），所以正常
    情况下它到达时目的地已经就位、直接返回 False。
    """
    try:
        if src.exists() and src.resolve() != dst.resolve() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return True
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# 数据迁移：逐项合并，只复制绝不删源
# ---------------------------------------------------------------------------
#
# 为什么是"逐项合并"而不是"选一个根"：旧实现用 `.writetest` 探针决定数据落点，
# 于是提权会话（updater 曾让新版继承管理员令牌）真的写进了
# ``C:\Program Files\FluentYTDL``，普通会话写进 ``~/Documents\FluentYTDL``。
# 两个根**都有真实数据**，是这套代码亲手造出来的分叉，只能逐项裁决。
#
# 为什么"绝不删源"：二进制回滚（core/updater.py Step 7 的 ROLLBACK）发生在新版
# READY 之前，而迁移也发生在 READY 之前。删了源，回滚后的旧版去旧路径找数据会
# 看到空目录 —— 用户看到的就是"更新把我的数据弄没了"。硬规则：
# **binary rollback 必须等价于 data-compatible rollback。**


def _record(msg: str) -> None:
    _MIGRATION_LOG.append(msg)


def _record_failure(msg: str) -> None:
    _MIGRATION_FAILURES.append(msg)
    _MIGRATION_LOG.append(f"[失败] {msg}")


def _record_conflict(msg: str) -> None:
    _MIGRATION_CONFLICTS.append(msg)
    _MIGRATION_LOG.append(f"[冲突] {msg}")


def take_migration_report() -> tuple[list[str], list[str], list[str]]:
    """取走并清空迁移日志 / 失败 / 冲突三个队列。

    返回 ``(log, failures, conflicts)``。由 `utils/startup_info.py::log_startup_info()`
    回放到 loguru（paths.py 不能 import loguru —— logger.py 在导入期就调
    `user_data_dir()`，反向 import 会成环）。

    **不清空 `_MIGRATION_OK`**：`log_startup_info()` 比 `finalize_startup()` 先跑，
    若把成败标志一起取走，`commit_migration_marker()` 就会在有失败的情况下照样
    落 `.migrated_v2`，正好绕开"零失败才写"这条规则。
    """
    log, failures, conflicts = (
        list(_MIGRATION_LOG),
        list(_MIGRATION_FAILURES),
        list(_MIGRATION_CONFLICTS),
    )
    _MIGRATION_LOG.clear()
    _MIGRATION_FAILURES.clear()
    _MIGRATION_CONFLICTS.clear()
    return log, failures, conflicts


def commit_migration_marker(app_name: str = "FluentYTDL") -> bool:
    """写出 `.migrated_v2`，宣告"这一版证明自己能跑，迁移结果算数了"。

    由 `utils/update_signal.py::finalize_startup()` 在关键服务全部就绪后调用，
    **绝不在 `migrate_user_data()` 里写**。两个条件缺一不可：

    * **零失败才写** —— 失败往往是"文件被占用"这种瞬时原因，下次启动该继续 retry。
      已成功的项因"平局归目的地"天然幂等，重跑不会产生副作用。
    * **READY 才写** —— 新版若在 READY 前崩溃并被 updater 回滚，标记不能留下。
      否则旧版接着往旧路径写数据，而下次更新看到标记就跳过迁移、直接采用陈旧
      副本 —— 那是静默的数据丢失。

    `_MIGRATION_OK is None` 表示本次启动没跑过迁移（标记已存在），无事可做。
    """
    if _MIGRATION_OK is not True:
        return False
    try:
        marker = user_data_dir(app_name) / MIGRATION_MARKER
        marker.write_text(
            "FluentYTDL 数据迁移已完成。删除本文件会让下次启动重新扫描旧数据位置。\n",
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


#: 超过这个大小的文件不做哈希，退回 size+mtime。参与迁移的文件（config.json、
#: 两个 json 缓存、单个日志）远在这条线以下，所以实际上总是走精确比较。
_HASH_MAX_BYTES = 8 * 1024 * 1024


def _file_digest(path: Path) -> str | None:
    try:
        import hashlib

        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _same_file(a: Path, b: Path) -> bool:
    """两个文件是否"同一份"。

    **size 相同就算同一份是不够的。** 落选者一旦被判成"与胜者相同"就整个丢弃，
    所以这个判定错一次就是一次静默的数据丢失 —— 而 size 撞车太容易了
    （`{"from": "a"}` 与 `{"from": "b"}` 字节数相同）。因此小文件一律比哈希。

    mtime 只在超大文件上兜底：`copy2` 保留 mtime，所以对"我们自己搬过去的副本"
    这条路径仍然恒成立，不破坏幂等。
    """
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    if sa.st_size != sb.st_size:
        return False
    if sa.st_size <= _HASH_MAX_BYTES:
        da = _file_digest(a)
        return da is not None and da == _file_digest(b)
    return abs(sa.st_mtime - sb.st_mtime) <= _MTIME_TOLERANCE


#: SQLite 的伴生文件。参与拷贝，但**不参与指纹比较** —— 见 `_tree_signature()`。
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _tree_signature(root: Path) -> tuple[int, int, float]:
    """目录指纹：(文件数, 总字节数, 最大 mtime)，SQLite 伴生文件不计入。

    最大 mtime 是免费的强化 —— 遍历时本来就在 stat 每个文件，而 `copy2` 保留
    mtime 意味着副本的指纹与源恒等，不破坏幂等。

    **为什么排除 `-wal` / `-shm`**：读 `MAX(updated_at)` 需要打开数据库，而最后
    一个连接关闭时 SQLite 会做一次 passive checkpoint 并删掉 `-wal`/`-shm`。
    也就是说"探测一下有多新"这个纯读操作会改变目录的文件数 —— 把伴生文件计进
    指纹，同一份数据在探测前后会得出两个不同的指纹。
    """
    count = 0
    total = 0
    newest = 0.0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(_SQLITE_SIDECARS):
                continue
            try:
                st = (Path(dirpath) / name).stat()
            except OSError:
                continue
            count += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return count, total, newest


def _same_tree(a: Path, b: Path) -> bool:
    """两个目录是否"同一份"。实际只会用在 `state/` 上。

    判错的代价是静默丢数据（落选者被当成胜者的副本直接丢弃），所以顺序是：

    1. (文件数, 总字节数) 不等 → 一定不同，收工。
    2. 两边都读得出逻辑时间 → **只认逻辑时间**。理由有两条，都是硬的：
       · size 在 SQLite 的页对齐下极易撞车 —— 两个内容完全不同的库常常字节数
         相同（页数一样），光靠 size 分辨不出来；
       · 读逻辑时间本身会触发 checkpoint 并刷新 mtime，所以"上一轮搬过去的那份"
         与"这一轮刚复制出来的同一份"mtime 必然不同 —— 只看 mtime 的话每次启动
         都会判成一次新冲突，往磁盘里又塞一份副本、又弹一次 InfoBar。
       逻辑时间对这两条都免疫：它是 checkpoint 不变量，也是真正的内容指纹。
    3. 逻辑时间读不出来（老表结构 / 空库 / 损坏）→ 才退回 mtime 容差。
    """
    ca, ta, ma = _tree_signature(a)
    cb, tb, mb = _tree_signature(b)
    if (ca, ta) != (cb, tb):
        return False
    la = _db_logical_time(_state_dir_db(a))
    lb = _db_logical_time(_state_dir_db(b))
    if la is not None and lb is not None:
        return abs(la - lb) <= _MTIME_TOLERANCE
    return abs(ma - mb) <= _MTIME_TOLERANCE


def _same_item(a: Path, b: Path) -> bool:
    if not a.exists() or not b.exists():
        return False
    if a.is_dir() != b.is_dir():
        return False
    return _same_tree(a, b) if a.is_dir() else _same_file(a, b)


def _mtime_of(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _sqlite_query(db_path: Path, sql: str) -> tuple | None:
    """对 `db_path` 跑一条只返回一行的查询，任何异常都吞掉返回 None。

    调用方保证 `db_path` 位于**可写的**位置（legacy 候选先复制到 `.migrate_tmp/`
    再查），这样 SQLite 打开时能正常重放 WAL —— 只读打开一个带 `-wal` 却没有
    `-shm` 的库会直接失败，而 `immutable=1` 又会整个忽略 WAL、读到陈旧数据。
    """
    if not db_path.exists():
        return None
    import sqlite3

    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        return conn.execute(sql).fetchone()
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _db_integrity_ok(db_path: Path) -> bool:
    """`PRAGMA integrity_check`。库不存在算通过（没有库不等于库坏了）。"""
    if not db_path.exists():
        return True
    row = _sqlite_query(db_path, "PRAGMA integrity_check")
    return bool(row) and str(row[0]).lower() == "ok"


def _db_logical_time(db_path: Path) -> float | None:
    """`SELECT MAX(updated_at) FROM tasks`，读不到返回 None。

    **为什么不能只看主库 mtime**：WAL 模式下最新事务可能整个还躺在 `-wal` 里，
    主库 mtime 几乎不动 —— 只比主库 mtime 会把陈旧库选成胜者。逻辑时间是唯一
    可靠的"这个库有多新"。`storage/task_db.py:80` 声明 `updated_at REAL NOT NULL`，
    存的是 epoch 秒，所以它与 mtime 同量纲、同容差可用。

    返回 None 的三种情况调用方一视同仁（表结构老 / 库损坏 / 库为空）：都表示
    "这个候选给不出逻辑时间"，于是整批退化为物理时间比较。
    """
    row = _sqlite_query(db_path, "SELECT MAX(updated_at) FROM tasks")
    if not row or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def _state_dir_db(state_dir: Path) -> Path:
    return state_dir / "tasks" / "tasks.db"


def _state_wall_time(state_dir: Path) -> float:
    """`tasks.db` 的物理时间：`max(mtime(db), mtime(db-wal))`。

    `-shm` **不参与** —— 它是共享内存索引，跟业务新旧无关。
    """
    db = _state_dir_db(state_dir)
    return max(_mtime_of(db), _mtime_of(db.with_name(db.name + "-wal")))


def _rank_state_candidates(staged: dict[str, Path]) -> dict[str, float]:
    """给每个 `state/` 候选算一个可比较的"新旧分"。

    逻辑时间优先，但**必须所有候选都拿得到逻辑时间才用它** —— 逻辑时间（业务
    epoch）与 wall time（文件 mtime）不是同一个标尺，混着比毫无意义。有任何一个
    候选读不出 `MAX(updated_at)`（表结构老、库损坏、库为空），整批一起退化为
    物理时间。
    """
    logical = {tag: _db_logical_time(_state_dir_db(p)) for tag, p in staged.items()}
    if logical and all(v is not None for v in logical.values()):
        return {tag: float(v) for tag, v in logical.items() if v is not None}
    return {tag: _state_wall_time(p) for tag, p in staged.items()}


def _root_tag(root: Path, new_dir: Path, app_name: str) -> str:
    """候选根 → 稳定的 tag。**推导而非计数**，同一台机器多次运行必然同 tag。

    tag 直接决定 conflict 目录名（`legacy_conflict_<tag>/`），带序号或时间戳的
    命名会让最容易触发回滚的那批用户磁盘里长出一堆 `legacy_conflict_2/`。
    """
    if root == new_dir:
        return "dest"
    try:
        if root.resolve() == old_user_data_dir(app_name).resolve():
            return "documents"
    except OSError:
        pass
    try:
        if root.resolve() == frozen_app_dir().resolve():
            return "install"
    except OSError:
        pass
    return "legacy"


def _discard(path: Path) -> None:
    """删掉一份暂存副本（文件或目录），失败静默。"""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _copy_item(src: Path, dst: Path) -> None:
    """复制单项（文件或整目录）到 dst，覆盖已有内容。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def _verify_copy(src: Path, dst: Path) -> bool:
    """校验副本可用：指纹一致，且（目录时）`tasks.db` 过得了完整性检查。

    校验失败的候选**整个退出裁决**，目的地保持不动 —— 与"拿一份可能损坏的库覆盖
    掉能用的那份"相比，这是唯一安全的方向。
    """
    if not dst.exists():
        return False
    if src.is_dir():
        cs, ts, _ = _tree_signature(src)
        cd, td, _ = _tree_signature(dst)
        if (cs, ts) != (cd, td):
            return False
        return _db_integrity_ok(_state_dir_db(dst))
    return _same_file(src, dst)


def _install_item(staged: Path, target: Path) -> None:
    """把暂存副本原子落到最终位置（同卷 `os.replace`）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _discard(target)
    os.replace(staged, target)


def _preserve_loser(staged: Path, new_dir: Path, tag: str, item: str, winner: Path) -> bool:
    """把落选的一份存进 `legacy_conflict_<tag>/<item>`。返回"落选者是否已安全交代"。

    返回 False 只有一种情况：确实需要保留、但保留失败了。调用方据此**放弃覆盖**
    目的地 —— 保不住旧的就别动新的，否则一次失败的保留会直接吃掉用户数据。

    三条硬规则都落在这个函数里：

    1. **路径确定性** —— 目标只由 (tag, item) 决定，与"第几次迁移"无关。禁止
       `_2` / `_3` / 时间戳 / uuid 后缀：迁移**会重跑**（标记延迟到 READY 就是为
       了让它重跑），避让式命名每跑一次就多一份副本。
    2. **与胜者相同就整个跳过** —— 上一轮我们自己把它装到胜者位置，`copy2` 保留
       了 size+mtime，所以重跑时"落选者 == 胜者"是常态，必须是彻底的 no-op
       （连冲突都不记，否则每次启动都弹一次 InfoBar）。
    3. **写前先比目标** —— 目标已存在且是同一份 → no-op；不同 → **覆盖同一个
       路径**并记一次冲突。
    """
    if _same_item(staged, winner):
        return True
    target = new_dir / f"legacy_conflict_{tag}" / item
    if target.exists() and _same_item(staged, target):
        return True
    try:
        _install_item(staged, target)
        _record_conflict(f"{item}（来自 {tag}）已保留为 {target}")
        return True
    except OSError as e:
        _record_failure(f"保留落选的 {item}（来自 {tag}）失败: {e}")
        return False


def _merge_logs(staged_logs: dict[str, Path], dest_logs: Path) -> bool:
    """合并日志目录 —— 日志天然可叠加，唯一需要小心的是重名。返回是否新增过文件。

    * 目的地无同名 → 直接拿过来
    * 目的地有同名且 size+mtime 一致 → 跳过（就是我们上次搬过去的那份）
    * 目的地有同名但不一致 → 落到 `<stem>_<tag><suffix>`，**固定后缀绝不递增**
    """
    merged = False
    for tag, src_dir in staged_logs.items():
        for src in sorted(src_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(src_dir)
            direct = dest_logs / rel
            if not direct.exists():
                try:
                    direct.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, direct)
                    merged = True
                except OSError as e:
                    _record_failure(f"迁移日志 {rel}（来自 {tag}）失败: {e}")
                continue
            if _same_file(src, direct):
                continue
            renamed = direct.with_name(f"{direct.stem}_{tag}{direct.suffix}")
            if renamed.exists() and _same_file(src, renamed):
                continue
            try:
                renamed.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, renamed)
                merged = True
                _record_conflict(f"日志 {rel}（来自 {tag}）与现有文件不同，已另存为 {renamed.name}")
            except OSError as e:
                _record_failure(f"迁移日志 {rel}（来自 {tag}）失败: {e}")
    return merged


def migrate_user_data(app_name: str = "FluentYTDL") -> bool:
    """把遗留位置的数据合并到 `user_data_dir()`。返回是否真的动过东西。

    候选根有三个，**目的地自己也是候选**：

    ==========================  ===========  ==================================
    候选                         tag          由来
    ==========================  ===========  ==================================
    `old_user_data_dir()`        documents    历史默认位置 ``~/Documents``
    `frozen_app_dir()`           install      提权会话写进安装目录的那份
    `user_data_dir()`            dest         目的地 —— 回滚后旧版又写过一轮
    ==========================  ===========  ==================================

    把目的地纳入裁决，是为了让"回滚 → 旧版继续写 → 再升级"这条路径上的数据能被
    正确重新选中；否则重跑会拿陈旧的 legacy 覆盖掉更新的目的地。

    全程 best-effort：任何单项失败只记录，**绝不抛异常、绝不阻塞启动**。程序照常
    以新目录启动，数据仍在旧位置未丢。
    """
    global _MIGRATION_OK

    new_dir = user_data_dir(app_name)
    if (new_dir / MIGRATION_MARKER).exists():
        return False

    try:
        new_resolved = new_dir.resolve()
    except OSError:
        new_resolved = new_dir

    # 遗留候选：与目的地同一个目录的直接排除（便携版的 frozen_app_dir() 天然命中）
    legacy_roots: list[tuple[str, Path]] = []
    for root in (old_user_data_dir(app_name), frozen_app_dir()):
        try:
            if not root.exists() or root.resolve() == new_resolved:
                continue
        except OSError:
            continue
        tag = _root_tag(root, new_dir, app_name)
        if any(tag == t for t, _ in legacy_roots):
            continue
        legacy_roots.append((tag, root))

    pending = [
        (tag, root)
        for tag, root in legacy_roots
        if any((root / i).exists() for i in MIGRATION_ITEMS)
    ]
    if not pending:
        # 没有任何遗留数据 —— 全新安装的常态。标记为成功，避免每次启动都重扫。
        _MIGRATION_OK = True
        return False

    _record(f"数据迁移：目的地 {new_dir}，遗留候选 {[t for t, _ in pending]}")

    tmp_root = new_dir / MIGRATE_TMP_DIRNAME
    # 开工先清 —— 上一次可能崩在中途留了半份副本。
    shutil.rmtree(tmp_root, ignore_errors=True)

    changed = False
    try:
        # ── 阶段 1：先复制，后裁决 ──────────────────────────────────
        #
        # 顺序不能反。legacy 源可能位于只读的 Program Files，而 WAL 库的只读打开
        # 本身就容易失败；副本是我们自己的，SQLite 打开时会正常重放 WAL，
        # MAX(updated_at) 才看得到最新事务。
        #
        # 目的地（dest）**不复制**，就地参与裁决：它必然可写、必然不是只读源，
        # 而且此刻 task_db 还没连上（迁移跑在单实例锁之后、config_manager /
        # task_db 首次实例化之前），不存在锁竞争。省掉一次可能上百 MB 的拷贝。
        staged: dict[str, dict[str, Path]] = {}  # item -> {tag: path}
        for item in MIGRATION_ITEMS:
            per_item: dict[str, Path] = {}
            for tag, root in pending:
                src = root / item
                if not src.exists():
                    continue
                dst = tmp_root / tag / item
                try:
                    _copy_item(src, dst)
                except OSError as e:
                    _record_failure(f"复制 {item}（来自 {tag}）失败: {e}")
                    continue
                if not _verify_copy(src, dst):
                    _record_failure(f"{item}（来自 {tag}）副本校验未通过，该候选不参与裁决")
                    _discard(dst)
                    continue
                per_item[tag] = dst
            if per_item:
                staged[item] = per_item

        # ── 阶段 2：逐项裁决并落位 ──────────────────────────────────
        for item, per_tag in staged.items():
            dest_path = new_dir / item

            if item == "logs":
                # 日志天然可叠加，不做胜负判定
                changed = _merge_logs(per_tag, dest_path) or changed
                continue

            candidates = dict(per_tag)
            if dest_path.exists():
                candidates["dest"] = dest_path

            if item == "state":
                ranks = _rank_state_candidates(candidates)
            else:
                ranks = {tag: _mtime_of(p) for tag, p in candidates.items()}

            # **平局归目的地。** copy2 保留 mtime，所以"上一轮搬过去的那份"与"这一
            # 轮刚复制出来的同一份"必然打平 —— 重跑时平局是常态，必须解成 no-op。
            best = max(ranks.values(), default=0.0)
            if "dest" in ranks and best - ranks["dest"] <= _MTIME_TOLERANCE:
                winner_tag = "dest"
            else:
                winner_tag = max(ranks, key=lambda t: ranks[t])

            keep_losers = item in _CONFLICT_KEPT_ITEMS

            if winner_tag == "dest":
                _record(f"{item}: 目的地已是最新，保持不动")
                if keep_losers:
                    for tag, staged_path in per_tag.items():
                        _preserve_loser(staged_path, new_dir, tag, item, dest_path)
                continue

            # legacy 胜出 → 目的地原有内容先让位，再原子落新的那份。
            # **让位失败就整项放弃**：保不住旧的绝不覆盖新的，否则一次失败的保留
            # 会把用户目的地里那份直接吃掉 —— 而它可能是回滚后旧版写下的真数据。
            if dest_path.exists() and keep_losers:
                if not _preserve_loser(dest_path, new_dir, "dest", item, per_tag[winner_tag]):
                    _record(f"{item}: 无法保留目的地原有副本，本项跳过（下次启动重试）")
                    continue
            try:
                _install_item(per_tag[winner_tag], dest_path)
                changed = True
                _record(f"{item}: 采用来自 {winner_tag} 的版本")
            except OSError as e:
                _record_failure(f"安装 {item}（来自 {winner_tag}）失败: {e}")
                continue
            if keep_losers:
                for tag, staged_path in per_tag.items():
                    if tag != winner_tag:
                        _preserve_loser(staged_path, new_dir, tag, item, dest_path)

        # ── 阶段 3：面包屑（不删源！）──────────────────────────────
        #
        # **legacy source 一律不删。** 二进制回滚（core/updater.py Step 7 的
        # ROLLBACK）发生在新版 READY 之前，而迁移也在 READY 之前跑完。删了源，
        # 回滚后的旧版去旧路径找数据只会看到空目录 —— 那正是用户说的"更新把我的
        # 数据弄没了"。硬规则：binary rollback 必须等价于 data-compatible rollback。
        # 旧位置的清理留给将来某个"确认新版稳定运行 N 天后"的独立动作。
        breadcrumb_text = f"{new_dir}\n"
        for _tag, root in pending:
            crumb = root / MIGRATION_BREADCRUMB
            try:
                if crumb.exists() and crumb.read_text(encoding="utf-8") == breadcrumb_text:
                    continue
                crumb.write_text(breadcrumb_text, encoding="utf-8")
            except OSError:
                # Program Files 根写不进去很正常，best-effort。**不算失败** ——
                # 它只是面包屑，不影响数据完整性，算进失败会让标记永远写不出来。
                _record(f"无法在 {root} 写下 {MIGRATION_BREADCRUMB}（只读位置，可忽略）")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    _MIGRATION_OK = not _MIGRATION_FAILURES
    if _MIGRATION_FAILURES:
        _record(
            f"迁移有 {len(_MIGRATION_FAILURES)} 项失败，下次启动将重试（不写 {MIGRATION_MARKER}）"
        )
    # 冲突副本也是"动过东西"。`_preserve_loser()` 返回的是"已安全交代"，其中包含
    # 纯 no-op（落选者与胜者本就是同一份），所以不能拿它当"写过"用 —— 那会让每次
    # 重跑都报告 changed=True，而重跑的正确答案恰恰是 False。
    return changed or bool(_MIGRATION_CONFLICTS)


def resource_path(*parts: str) -> Path:
    # When frozen, resources live under sys._MEIPASS.
    base = Path(getattr(sys, "_MEIPASS", "")) if is_frozen() else project_root()
    return base.joinpath(*parts)


def frozen_internal_dir() -> Path:
    """Best-effort path to the PyInstaller onedir internal directory.

    In PyInstaller onedir builds, Python libs typically live under `_internal`.
    `sys._MEIPASS` often points to that folder, but we keep a fallback based on
    `sys.executable` to be robust across packaging variations.
    """

    if not is_frozen():
        return project_root()

    meipass = Path(getattr(sys, "_MEIPASS", "") or "")
    if str(meipass) and meipass.exists():
        return meipass

    exe_dir = Path(sys.executable).resolve().parent
    candidate = exe_dir / "_internal"
    if candidate.exists():
        return candidate
    return exe_dir


def frozen_app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return project_root()


def get_clean_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """获取一个干净的环境变量字典，剥离 PyInstaller 的 _MEIPASS 污染。

    PyInstaller 会在运行前将 _MEIPASS 注入到系统 PATH 的最前方。
    外部二进制工具 (如 ffmpeg, yt-dlp) 启动时如果加载了 _MEIPASS 中的同名 Qt/SSL DLL，
    会导致致命崩溃 (特别是 0 字节文件和静默闪退情况)。

    此函数会：
    1. 删除 PATH 中的所有 _MEIPASS / _MEIPASS2 路径片段
    2. 删除 OS 环境里的 _MEIPASS / _MEIPASS2 原始键
    """
    env = dict(base_env) if base_env is not None else os.environ.copy()

    if not is_frozen():
        return env

    meipass = getattr(sys, "_MEIPASS", "")
    meipass2 = getattr(sys, "_MEIPASS2", "")

    # 1. 删除特殊的 key
    env.pop("_MEIPASS", None)
    env.pop("_MEIPASS2", None)

    # 2. 清洗 PATH
    if "PATH" in env:
        paths = env["PATH"].split(os.pathsep)
        clean_paths = []
        for p in paths:
            if not p:
                continue
            try:
                # 忽略匹配的 _MEIPASS 路径
                if meipass and os.path.normpath(p).upper() == os.path.normpath(meipass).upper():
                    continue
                if meipass2 and os.path.normpath(p).upper() == os.path.normpath(meipass2).upper():
                    continue
            except Exception:
                pass
            clean_paths.append(p)
        env["PATH"] = os.pathsep.join(clean_paths)

    return env


def bundled_bin_dir() -> Path:
    # Legacy location (older builds): assets/bin
    # Note: depending on PyInstaller layout, assets may be placed next to the exe
    # rather than under sys._MEIPASS.
    p = resource_path("assets", "bin")
    if p.exists():
        return p
    p2 = frozen_app_dir() / "assets" / "bin"
    return p2


def find_bundled_executable(*relative_candidates: str) -> Path | None:
    """Find an executable shipped with the app.

    Examples:
    - find_bundled_executable("ffmpeg/ffmpeg.exe")
    - find_bundled_executable("js/deno.exe", "deno/deno.exe")
    """

    # Preferred locations (new layout):
    # - dist/_internal/ffmpeg
    # - dist/_internal/js_runtime
    internal = frozen_internal_dir()
    search_roots: list[Path] = [
        frozen_app_dir() / "bin",  # High priority: standard packaged bin folder
        internal / "ffmpeg",
        internal / "js_runtime",
        internal / "yt-dlp",
        internal / "assets" / "bin",
        bundled_bin_dir(),
    ]

    for rel in relative_candidates:
        rel_path = Path(rel)
        for root in search_roots:
            try:
                p = root / rel_path
                if p.exists():
                    return p
            except Exception:
                continue

        # Compatibility: if caller passes "ffmpeg/ffmpeg.exe", also try stripping prefix
        # for the new `internal/ffmpeg` layout.
        try:
            parts = rel_path.parts
            if (
                parts
                and parts[0].lower()
                in {"ffmpeg", "js", "node", "bun", "quickjs", "yt-dlp", "yt_dlp"}
                and len(parts) >= 2
            ):
                stripped = Path(*parts[1:])
                for root in search_roots:
                    try:
                        p = root / stripped
                        if p.exists():
                            return p
                    except Exception:
                        continue
        except Exception:
            pass

    return None


def locate_runtime_tool(*relative_candidates: str) -> Path:
    """Locate a required runtime tool.

    Priority:
    1) Explicit check in exe_dir/bin (standard packaged structure)
    2) `bin` directory adjacent to the frozen exe (generic search)
    3) system PATH (via `shutil.which`)
    If not found, raises FileNotFoundError.
    """

    # --- 0. Explicit High-Priority Check for Packaged Tools ---
    # This addresses issues where tools are definitely in `bin/` next to the exe
    # but generic logic might miss them due to path combination complexity.
    if is_frozen():
        exe_bin = frozen_app_dir() / "bin"
        if exe_bin.exists():
            for rel in relative_candidates:
                # Case A: bin/ffmpeg.exe
                p1 = exe_bin / Path(rel).name
                if p1.exists():
                    return p1.resolve()

                # Case B: bin/ffmpeg/ffmpeg.exe (if rel is "ffmpeg/ffmpeg.exe")
                p2 = exe_bin / rel
                if p2.exists():
                    return p2.resolve()

                # Case C: bin/ffmpeg/ffmpeg.exe (automatic subfolder guessing)
                # If searching for "ffmpeg.exe", try checking "bin/ffmpeg/ffmpeg.exe"
                name = Path(rel).name
                stem = Path(rel).stem  # e.g. "ffmpeg"
                p3 = exe_bin / stem / name
                if p3.exists():
                    return p3.resolve()

    # Build a list of candidate local roots to check.
    # Priority: current working directory (where user launched the exe),
    # then frozen exe dir / internal assets, then project-level assets (dev).
    local_roots: list[Path] = []

    # 0) current working directory - important for onefile builds launched from their folder
    try:
        cwd = Path.cwd()
        local_roots.append(cwd)
        local_roots.append(cwd / "bin")
        local_roots.append(cwd / "assets" / "bin")
    except Exception:
        pass

    # 1) frozen exe / internal locations
    exe_dir = frozen_app_dir()
    local_roots += [
        exe_dir,
        exe_dir / "bin",
        exe_dir / "assets" / "bin",
        frozen_internal_dir() / "assets" / "bin",
        frozen_internal_dir() / "ffmpeg",
        frozen_internal_dir() / "js_runtime",
    ]

    # 2) development/project locations
    pr = project_root()
    local_roots += [
        pr / "src" / "fluentytdl" / "assets" / "bin",
        pr / "assets" / "bin",
        pr / "bin",
    ]

    # 1) check local roots for the tool
    for root in local_roots:
        for rel in relative_candidates:
            rel_path = Path(rel)
            try:
                p = root / rel_path
                if p.exists():
                    return p.resolve()
            except Exception:
                pass
            try:
                p2 = root / rel_path.name
                if p2.exists():
                    return p2.resolve()
            except Exception:
                pass

    # 2) fallback to PATH
    for rel in relative_candidates:
        name = Path(rel).name
        try:
            found = shutil.which(name)
        except Exception:
            found = None
        if found:
            return Path(found).resolve()

    # not found
    raise FileNotFoundError(
        f"工具未找到: {relative_candidates}. 请将相应可执行文件放入 'bin' 目录，或将其加入系统 PATH，或在设置中指定路径。"
    )


def config_path() -> Path:
    return user_data_dir() / "config.json"


def legacy_config_path() -> Path:
    # Old versions always used repo root.
    return project_root() / "config.json"


def doc_path() -> Path:
    """Return the path to the documentation directory."""
    if is_frozen():
        # Frozen app: check relative to exe or internal
        exe_dir = frozen_app_dir()
        candidates = [
            exe_dir / "docs",
            frozen_internal_dir() / "docs",
            resource_path("docs"),
        ]
        for p in candidates:
            if p.exists():
                return p
        return exe_dir / "docs"  # Default fallback

    # Dev mode
    return project_root() / "docs"
