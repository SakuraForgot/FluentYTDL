import json
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..utils.paths import config_path, user_data_dir

logger = logging.getLogger(__name__)


class TaskDB:
    """
    单点写入的 SQLite 任务数据库 (WAL 模式)。
    负责存储所有任务的全生命周期状态 (queued, downloading, completed, error, paused)。
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_db()
            return cls._instance

    def _init_db(self):
        # Runtime task DB should live in a dedicated user-writable folder.
        # This avoids polluting repo/exe directories and works in Program Files installs.
        self.db_path = self._resolve_db_path()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()

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
        legacy = config_path().parent / "tasks.db"

        # One-time best-effort migration from old location.
        try:
            preferred.parent.mkdir(parents=True, exist_ok=True)
            if (
                legacy.exists()
                and legacy.resolve() != preferred.resolve()
                and not preferred.exists()
            ):
                shutil.copy2(legacy, preferred)
        except Exception:
            pass

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
                self._conn.commit()
            except sqlite3.Error as e:
                logger.error(f"Failed to create TaskDB tables: {e}")

    def insert_task(self, url: str, ydl_opts: dict) -> int:
        """
        插入一个新任务并返回其主键 ID
        """
        now = time.time()
        opts_json = json.dumps(ydl_opts, ensure_ascii=False)
        with self._write_lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO tasks (url, state, ydl_opts_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """,
                (url, "queued", opts_json, now, now),
            )
            task_id = cursor.lastrowid
            self._conn.commit()
            return task_id

    def update_task_status(self, task_id: int, state: str, progress: float, status_text: str):
        """更新任务的状态、进度和文本"""
        now = time.time()
        with self._write_lock:
            self._conn.execute(
                """
                UPDATE tasks
                SET state = ?, progress = ?, status_text = ?, updated_at = ?
                WHERE id = ?
            """,
                (state, progress, status_text, now, task_id),
            )
            self._conn.commit()

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
        with self._write_lock:
            self._conn.execute(
                """
                UPDATE tasks
                SET title = ?, thumbnail_url = ?, output_path = ?, file_size = ?, duration = ?, updated_at = ?
                WHERE id = ?
            """,
                (title, thumbnail_url, output_path, file_size, duration, now, task_id),
            )
            self._conn.commit()

    def update_task_result(self, task_id: int, output_path: str, file_size: int = 0):
        """更新最终输出路径和文件大小"""
        now = time.time()
        with self._write_lock:
            self._conn.execute(
                """
                UPDATE tasks
                SET output_path = ?, file_size = ?, updated_at = ?
                WHERE id = ?
            """,
                (output_path, file_size, now, task_id),
            )
            self._conn.commit()

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

    def delete_task(self, task_id: int):
        """删除任务"""
        with self._write_lock:
            self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._conn.commit()


task_db = TaskDB()
