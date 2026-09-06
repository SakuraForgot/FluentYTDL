import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..utils.logger import logger
from ..utils.paths import _migrate_file, config_path, old_user_data_dir, user_data_dir

# `tasks.state` 列的取值全集。写入方只有两处：`DownloadWorker.unified_status`
# （经 db_writer 落库）和 `DownloadManager.suspend_pending` 的 quality_guard 挂起。
# 拆成「终态 / 未完成」两组是因为**行来源的所有权是分开的**：
# 未完成态由 `download_manager.load_unfinished_tasks()` 在启动时一次性注入 UI，
# 终态由列表的 `fetchMore()` 分页补充 —— 两边不许交叉，否则同一任务会进两次。
#
# **反过来，两组都不含的状态会让任务从 UI 里彻底消失。** 两条加载路径分别只查
# `TERMINAL_STATES`（`ui/models/download_list_model.py`）和 `UNFINISHED_STATES`
# （`download_manager.load_unfinished_tasks()`），落在集合外的行谁也捞不到。
# `processing` 就踩过这个坑 —— 它是发射次数最多的状态（Merger / EmbedSubtitle /
# FFmpegMetadata / ThumbnailsConvertor / MoveFiles / SponsorBlock / 重试 …），
# 在 FFmpeg 合并期间关掉软件，那一行会永远停在 `processing`：任务从列表里没了，
# 半成品还躺在 sandbox 临时目录里。新增状态字符串时**必须**同步进这两个元组之一。
#
# `running` 是死词汇（全项目没有任何代码发射它），留在这里只为兼容老库里的历史行。
TERMINAL_STATES: tuple[str, ...] = ("completed", "error", "cancelled")
UNFINISHED_STATES: tuple[str, ...] = (
    "queued",
    "downloading",
    "parsing",
    "processing",
    "running",
    "paused",
    "quality_guard",
)


class TaskDB:
    """
    单点写入的 SQLite 任务数据库 (WAL 模式)。
    负责存储所有任务的全生命周期状态 (queued, downloading, completed, error, paused)。
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: str | Path | None = None):
        # db_path **只给测试传**：显式给路径就绕过单例，返回一个连到临时文件的独立实例，
        # 于是分页 / 统计的单元测试不必碰用户真实的 tasks.db。生产代码一律用无参形式。
        if db_path is not None:
            instance = super().__new__(cls)
            instance._init_db(Path(db_path))
            return instance
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_db()
            return cls._instance

    def __init__(self, db_path: str | Path | None = None) -> None:
        # 初始化全部发生在 __new__ → _init_db（单例只做一次）。这个空壳必须留着：
        # 没有它，object.__init__ 会因为多出来的 db_path 参数报 TypeError。
        pass

    def _init_db(self, db_path: Path | None = None):
        # Runtime task DB should live in a dedicated user-writable folder.
        # This avoids polluting repo/exe directories and works in Program Files installs.
        self.db_path = db_path if db_path is not None else self._resolve_db_path()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        # 事务深度**按线程**记。`batch()` 只让本线程的写入穿透掉「重复取锁 + 重复提交」，
        # 其他线程照旧在 `_write_lock` 上排队 —— 否则 B 线程会把自己的 UPDATE
        # 悄悄塞进 A 线程的事务里，并在 A 回滚时一起丢失。
        self._tx = threading.local()

        # 建立全局写连接
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # 自动提交模式，或者我们自己控事务
        )
        self._conn.row_factory = sqlite3.Row

        # 开启 WAL 模式提高并发性能
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")

        self._create_tables()

    def _resolve_db_path(self) -> Path:
        preferred = user_data_dir() / "state" / "tasks" / "tasks.db"

        # Migration from old Documents location
        old_docs_db = old_user_data_dir() / "state" / "tasks" / "tasks.db"
        _migrate_file(old_docs_db, preferred)

        # Migration from legacy config-adjacent location
        legacy = config_path().parent / "tasks.db"
        _migrate_file(legacy, preferred)

        return preferred

    def _create_tables(self):
        """初始化表结构"""
        with self._write_lock:
            try:
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        title TEXT DEFAULT '',
                        thumbnail_url TEXT DEFAULT '',
                        state TEXT NOT NULL DEFAULT 'queued',
                        progress REAL DEFAULT 0.0,
                        status_text TEXT DEFAULT '',
                        output_path TEXT DEFAULT '',
                        file_size INTEGER DEFAULT 0,
                        duration INTEGER DEFAULT 0,
                        ydl_opts_json TEXT DEFAULT '{}',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                """)
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS notifications (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        type TEXT NOT NULL,
                        severity TEXT NOT NULL DEFAULT 'info',
                        title TEXT NOT NULL,
                        message TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        is_read INTEGER DEFAULT 0,
                        related_task_id INTEGER DEFAULT 0,
                        metadata_json TEXT DEFAULT '{}'
                    )
                """)
                self._migrate_tables()
                self._conn.commit()
            except sqlite3.Error as e:
                logger.error(f"Failed to create TaskDB tables: {e}")

    def _migrate_tables(self):
        """执行数据库迁移"""
        try:
            cursor = self._conn.cursor()
            cursor.execute("PRAGMA table_info(tasks)")
            columns = [col[1] for col in cursor.fetchall()]

            if "actual_height" not in columns:
                cursor.execute("ALTER TABLE tasks ADD COLUMN actual_height INTEGER DEFAULT 0")
            if "target_height" not in columns:
                cursor.execute("ALTER TABLE tasks ADD COLUMN target_height INTEGER DEFAULT 0")
            if "quality_deviation" not in columns:
                cursor.execute("ALTER TABLE tasks ADD COLUMN quality_deviation TEXT DEFAULT ''")
            # 观测标识：run 开始时写入，供**启动恢复审计**给出 `run=A outcome=interrupted`。
            # 只存内存的话，重启后只知道 `task=42 previous_state=downloading`，不知道刚死的
            # 是哪个 run —— 时间线闭不上环，`logs/traces/` 里的 jsonl 也对不上号。
            if "last_session_id" not in columns:
                cursor.execute("ALTER TABLE tasks ADD COLUMN last_session_id TEXT DEFAULT ''")
            if "last_run_id" not in columns:
                cursor.execute("ALTER TABLE tasks ADD COLUMN last_run_id TEXT DEFAULT ''")
            if "last_flow_id" not in columns:
                cursor.execute("ALTER TABLE tasks ADD COLUMN last_flow_id TEXT DEFAULT ''")

            # keyset 分页与计数徽标的支撑索引。原来这张表**一个索引都没有**，
            # 而 `query_tasks` 要按 `(updated_at, id)` 倒序翻页、`count_tasks_by_state`
            # 要按 state 分组 —— 没索引就是每页一次全表扫 + 一次临时排序。
            # `(updated_at, id)`：不限状态或多状态取并集时按它倒序走，边走边过滤 state，
            #   到 LIMIT 就能收尾，不需要 temp b-tree。
            # `(state, updated_at, id)`：单状态筛选走它是覆盖扫描，GROUP BY state 同理。
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC, id DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_state_updated "
                "ON tasks(state, updated_at DESC, id DESC)"
            )
        except sqlite3.Error as e:
            logger.error(f"Database migration failed: {e}")

    # === 写入事务 ===

    @contextmanager
    def _writing(self) -> Iterator[None]:
        """单条写入的标准包装：取锁 → 执行 → commit。

        本线程已经在 `batch()` 里时**原地穿透**：不重复取锁（`_write_lock` 是不可重入的
        `Lock`，重复取就是自死锁），也不 commit（提交时机归 `batch()`）。
        """
        if getattr(self._tx, "depth", 0) > 0:
            yield
            return
        with self._write_lock:
            yield
            self._conn.commit()

    @contextmanager
    def batch(self) -> Iterator[None]:
        """把区块内的多次写入合并成**一个**事务提交。

        状态更新约 5Hz/任务，逐条 commit 就是每秒每任务一次 WAL 提交；融合之后行数变多，
        这是最直接的写放大来源。区块内任何异常都整批回滚。

        嵌套调用会穿透（不重开 BEGIN），所以 `batch()` 里再调用带 `batch()` 的方法是安全的。
        """
        if getattr(self._tx, "depth", 0) > 0:
            yield
            return
        with self._write_lock:
            self._tx.depth = 1
            try:
                self._conn.execute("BEGIN")
                yield
            except BaseException:
                try:
                    self._conn.rollback()
                except sqlite3.Error as e:
                    logger.warning(f"TaskDB batch rollback failed: {e}")
                raise
            else:
                self._conn.commit()
            finally:
                self._tx.depth = 0

    def insert_task(self, url: str, ydl_opts: dict) -> int:
        """
        插入一个新任务并返回其主键 ID
        """
        now = time.time()
        opts_json = json.dumps(ydl_opts, ensure_ascii=False)
        with self._writing():
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO tasks (url, state, ydl_opts_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """,
                (url, "queued", opts_json, now, now),
            )
            task_id = cursor.lastrowid
            return task_id

    def update_task_status(self, task_id: int, state: str, progress: float, status_text: str):
        """更新任务的状态、进度和文本"""
        now = time.time()
        with self._writing():
            self._conn.execute(
                """
                UPDATE tasks
                SET state = ?, progress = ?, status_text = ?, updated_at = ?
                WHERE id = ?
            """,
                (state, progress, status_text, now, task_id),
            )

    def update_task_metadata(
        self,
        task_id: int,
        title: str,
        thumbnail_url: str,
        output_path: str = "",
        file_size: int = 0,
        duration: int = 0,
    ):
        """更新解析后收集到的视频元数据"""
        now = time.time()
        with self._writing():
            self._conn.execute(
                """
                UPDATE tasks
                SET title = ?, thumbnail_url = ?, output_path = ?, file_size = ?, duration = ?, updated_at = ?
                WHERE id = ?
            """,
                (title, thumbnail_url, output_path, file_size, duration, now, task_id),
            )

    def update_task_result(self, task_id: int, output_path: str, file_size: int = 0):
        """更新最终输出路径和文件大小"""
        now = time.time()
        with self._writing():
            self._conn.execute(
                """
                UPDATE tasks
                SET output_path = ?, file_size = ?, updated_at = ?
                WHERE id = ?
            """,
                (output_path, file_size, now, task_id),
            )

    def update_task_opts(self, task_id: int, ydl_opts: dict) -> None:
        """改写任务的 ydl_opts 快照。

        「自动降档重试」会把 `format` 换成低一档的表达式。`create_worker` 走
        `restore_db_id` 分支时**不写** `ydl_opts_json`（只有新建任务才 `insert_task`），
        所以不在这里落库的话，下次重启按快照重下又会用回原来那个拿不到的档位。
        """
        now = time.time()
        opts_json = json.dumps(ydl_opts, ensure_ascii=False)
        try:
            with self._writing():
                self._conn.execute(
                    "UPDATE tasks SET ydl_opts_json = ?, updated_at = ? WHERE id = ?",
                    (opts_json, now, task_id),
                )
        except sqlite3.Error as e:
            logger.error(f"Failed to update task opts: {e}")

    def update_task_quality(
        self, task_id: int, actual_height: int, target_height: int, deviation: str
    ) -> None:
        """更新任务质量偏差记录"""
        try:
            with self._writing():
                self._conn.execute(
                    "UPDATE tasks SET actual_height = ?, target_height = ?, quality_deviation = ? WHERE id = ?",
                    (actual_height, target_height, deviation, task_id),
                )
        except sqlite3.Error as e:
            logger.error(f"Failed to update task quality: {e}")

    def update_task_run_identity(
        self, task_id: int, session_id: str, run_id: str, flow_id: str = ""
    ) -> None:
        """记下这个任务当前正跑在哪个 run 上。

        run 开始时调一次即可。启动恢复审计读它来产出
        `task=42 run=A outcome=interrupted`，随后新 run 才接得上 —— 没有这一列，
        被强杀的那次执行在日志里就是个断头。

        **刻意不更新 `updated_at`**：这只是观测标识，不是业务状态变化，
        改动时间戳会把任务在列表里顶到最前面。
        """
        try:
            with self._writing():
                self._conn.execute(
                    "UPDATE tasks SET last_session_id = ?, last_run_id = ?, last_flow_id = ? "
                    "WHERE id = ?",
                    (session_id, run_id, flow_id, task_id),
                )
        except sqlite3.Error as e:
            logger.error(f"Failed to update task run identity: {e}")

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        """获取单个任务详情"""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_all_tasks(self) -> list[dict[str, Any]]:
        """获取所有任务（通常用于启动时恢复列表）"""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM tasks ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]

    # === 分页 / 统计查询 ===

    def query_tasks(
        self,
        states: Sequence[str] | None = None,
        limit: int = 100,
        before_key: tuple[float, int] | None = None,
    ) -> list[dict[str, Any]]:
        """按 `(updated_at, id)` 倒序分页取任务行。

        **keyset 分页，不用 OFFSET。** 两个理由，第二个是硬的：

        1. `LIMIT ? OFFSET ?` 每翻一页都要重新数过前面所有行，第 N 页是 O(N·页大小)。
        2. 下载中的任务每秒都在改 `updated_at`，行序会在用户翻页途中变化 —— OFFSET
           在动态排序上必然漏行或重复行。keyset 用「上一页最后一行的键」定位，免疫。

        `before_key` 传上一页最后一行的 `(updated_at, id)`，首页传 None。
        `states=None` 表示不限状态；传空序列直接返回空列表（不是「不限」）。

        返回的是 `dict`，键就是表列名 —— 与 `get_task` / `get_all_tasks` 一致。
        """
        sql = ["SELECT * FROM tasks"]
        where: list[str] = []
        params: list[Any] = []

        if states is not None:
            state_list = list(states)
            if not state_list:
                return []
            where.append(f"state IN ({','.join('?' * len(state_list))})")
            params.extend(state_list)

        if before_key is not None:
            # 元组比较手写展开：行值比较 `(a,b) < (?,?)` 要 SQLite 3.15+，
            # 而 Python 自带的 sqlite3 版本随解释器构建浮动，不值得赌。
            where.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
            params.extend([before_key[0], before_key[0], before_key[1]])

        if where:
            sql.append("WHERE " + " AND ".join(where))
        # id 必须参与排序：`updated_at` 是 REAL，批量恢复时同一个 time.time() 会落到
        # 多行上，只按 updated_at 排序在并列处的顺序未定义 —— 那正是 keyset 漏行的入口。
        sql.append("ORDER BY updated_at DESC, id DESC LIMIT ?")
        params.append(max(0, int(limit)))

        try:
            cursor = self._conn.cursor()
            cursor.execute(" ".join(sql), params)
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"query_tasks failed: {e}")
            return []

    @staticmethod
    def page_key(row: dict[str, Any]) -> tuple[float, int]:
        """从一行里取出翻页游标，交给下一次 `query_tasks(before_key=...)`。"""
        return (float(row.get("updated_at") or 0.0), int(row.get("id") or 0))

    def count_tasks_by_state(self) -> dict[str, int]:
        """一条 `GROUP BY` 拿到各状态的行数（计数徽标用，不物化任何行）。"""
        try:
            cursor = self._conn.cursor()
            cursor.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")
            return {str(row["state"]): int(row["n"]) for row in cursor.fetchall()}
        except sqlite3.Error as e:
            logger.error(f"count_tasks_by_state failed: {e}")
            return {}

    def delete_task(self, task_id: int):
        """删除任务"""
        with self._writing():
            self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def delete_tasks(self, task_ids: Sequence[int]) -> int:
        """批量删除，一个事务提交。返回请求删除的条数。"""
        ids = [int(i) for i in task_ids]
        if not ids:
            return 0
        with self.batch():
            self._conn.executemany("DELETE FROM tasks WHERE id = ?", [(i,) for i in ids])
        return len(ids)


task_db = TaskDB()
