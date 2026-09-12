import json
import sqlite3
import time

from PySide6.QtCore import QObject, Signal

from fluentytdl.utils.localized_log import log_text

from ..storage.task_db import task_db
from ..utils.logger import logger
from .notification_model import Notification
from .notification_text import capture_fields


class NotificationCenter(QObject):
    """
    消息中心：负责通知的推送、存储（SQLite）和分发（Signal）。
    单例模式。
    """

    # 新通知到达信号
    notification_added = Signal(Notification)
    # 通知状态变更信号（如已读）
    notification_updated = Signal(int)  # 传递通知 ID
    # 通知数量变化信号（未读数量，总数量）
    unread_count_changed = Signal(int)
    announcement_requested = Signal(dict)
    announcement_refresh_requested = Signal()

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self):
        # 避免 QObject.__init__ 被多次调用
        if getattr(self, "_initialized", False):
            return
        super().__init__()
        self._initialized = True

        self.db = task_db

    def push(self, notification: Notification) -> int:
        """推送一条新通知，持久化到数据库并广播。"""
        if notification.timestamp <= 0:
            notification.timestamp = time.time()

        fields = capture_fields(notification)
        if fields:
            notification.metadata = {**notification.metadata, "display_text": fields}
        metadata_json = json.dumps(notification.metadata, ensure_ascii=False)

        try:
            with self.db._write_lock:
                cursor = self.db._conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO notifications 
                    (type, severity, title, message, timestamp, is_read, related_task_id, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        notification.type,
                        notification.severity,
                        notification.title,
                        notification.message,
                        notification.timestamp,
                        1 if notification.is_read else 0,
                        notification.related_task_id,
                        metadata_json,
                    ),
                )
                notif_id = cursor.lastrowid
                self.db._conn.commit()

            notification.id = notif_id
            self.notification_added.emit(notification)
            self._emit_unread_count()

            log_text(
                logger,
                "info",
                "消息中心: 收到新通知 [{0}] {1}",
                notification.severity,
                notification.title,
            )
            return notif_id

        except sqlite3.Error as e:
            log_text(logger, "error", "推送通知失败: {0}", e)
            return 0

    def get_all(self, limit: int = 100, offset: int = 0) -> list[Notification]:
        """获取通知列表（降序）。"""
        try:
            cursor = self.db._conn.cursor()
            cursor.execute(
                """
                SELECT id, type, severity, title, message, timestamp, is_read, related_task_id, metadata_json
                FROM notifications
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = cursor.fetchall()
            return [self._row_to_notification(row) for row in rows]
        except sqlite3.Error as e:
            log_text(logger, "error", "读取通知失败: {0}", e)
            return []

    def get_unread_count(self) -> int:
        """获取未读通知数量。"""
        try:
            cursor = self.db._conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM notifications WHERE is_read = 0")
            row = cursor.fetchone()
            return row[0] if row else 0
        except sqlite3.Error as e:
            log_text(logger, "error", "获取未读通知数失败: {0}", e)
            return 0

    def mark_as_read(self, notif_id: int):
        """标记单个通知为已读。"""
        try:
            with self.db._write_lock:
                self.db._conn.execute(
                    "UPDATE notifications SET is_read = 1 WHERE id = ?", (notif_id,)
                )
                self.db._conn.commit()
            self.notification_updated.emit(notif_id)
            self._emit_unread_count()
        except sqlite3.Error as e:
            log_text(logger, "error", "标记通知已读失败: {0}", e)

    def mark_all_as_read(self):
        """标记所有通知为已读。"""
        try:
            with self.db._write_lock:
                self.db._conn.execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")
                self.db._conn.commit()
            self.notification_updated.emit(0)  # 0代表所有
            self._emit_unread_count()
        except sqlite3.Error as e:
            log_text(logger, "error", "标记全部通知已读失败: {0}", e)

    def clear_all(self):
        """清空所有通知。"""
        try:
            with self.db._write_lock:
                self.db._conn.execute("DELETE FROM notifications")
                self.db._conn.commit()
            self.notification_updated.emit(-1)  # -1代表清除
            self._emit_unread_count()
        except sqlite3.Error as e:
            log_text(logger, "error", "清空通知失败: {0}", e)

    def delete_notification(self, notif_id: int):
        """删除指定通知。"""
        try:
            with self.db._write_lock:
                self.db._conn.execute("DELETE FROM notifications WHERE id = ?", (notif_id,))
                self.db._conn.commit()
            self.notification_updated.emit(notif_id)
            self._emit_unread_count()
        except sqlite3.Error as e:
            log_text(logger, "error", "删除通知失败: {0}", e)

    def _row_to_notification(self, row: sqlite3.Row) -> Notification:
        try:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        except Exception:
            metadata = {}

        return Notification(
            id=row["id"],
            type=row["type"],
            severity=row["severity"],
            title=row["title"],
            message=row["message"],
            timestamp=row["timestamp"],
            is_read=bool(row["is_read"]),
            related_task_id=row["related_task_id"],
            metadata=metadata,
        )

    def _emit_unread_count(self):
        count = self.get_unread_count()
        self.unread_count_changed.emit(count)


# 全局单例
notification_center = NotificationCenter()
