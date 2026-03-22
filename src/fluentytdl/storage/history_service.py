"""
下载历史记录服务 (适配器模式 - 代理到 TaskDB)

本文件原来是一个独立的 SQLite 服务，现在退化为一个针对 TaskDB 的只读适配器，
专门过滤出 `state IN ('completed', 'error')` 的任务记录以兼容老版本的 UI 模型。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any

from ..utils.logger import logger
from .task_db import task_db

# ---------------------------------------------------------------------------
# 数据模型 (UI层依旧依赖这个模型)
# ---------------------------------------------------------------------------


@dataclass
class HistoryRecord:
    video_id: str
    url: str
    title: str
    output_path: str
    file_size: int = 0
    thumbnail_url: str = ""
    duration: int = 0
    format_note: str = ""
    download_time: float = 0.0
    file_exists: bool = True
    db_id: int = 0  # 映射回 TaskDB 主键，方便删除操作

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("file_exists", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryRecord:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


@dataclass
class HistoryGroup:
    video_id: str
    title: str
    records: list[HistoryRecord]

    @property
    def latest(self) -> HistoryRecord | None:
        return self.records[0] if self.records else None

    @property
    def any_exists(self) -> bool:
        return any(r.file_exists for r in self.records)


_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")


def extract_video_id(url: str) -> str:
    m = _YT_ID_RE.search(url or "")
    return m.group(1) if m else ""


# 无效回调注册：因为现在是实时的 TaskDB，UI 层不再依赖此回调试图新增逻辑
_on_add_callbacks: list = []


def on_history_added(callback) -> None:
    pass


def on_history_updated(callback) -> None:
    pass


# ---------------------------------------------------------------------------
# HistoryService (代理)
# ---------------------------------------------------------------------------


class HistoryService:
    def __init__(self):
        pass

    def add(self, *args, **kwargs):
        # UI 写入逻辑已摘除，直接忽视
        pass

    def _convert_row_to_record(self, row: dict[str, Any]) -> HistoryRecord:
        url = row["url"]
        opts = json.loads(row["ydl_opts_json"] or "{}")

        # 组装 format_note
        fmt_note = opts.get("__fluentytdl_format_note", "")
        if not fmt_note:
            fmt = opts.get("format", "")
            if "1080" in fmt:
                fmt_note = "1080p"
            elif "720" in fmt:
                fmt_note = "720p"
            elif "480" in fmt:
                fmt_note = "480p"
            else:
                m = re.search(r"height<=(\d+)", fmt)
                if m:
                    fmt_note = f"{m.group(1)}p"

        out = row["output_path"] or ""
        ext = os.path.splitext(out)[1].lstrip(".").upper() if out else ""
        if ext and ext.lower() not in fmt_note.lower():
            if fmt_note:
                fmt_note = f"{fmt_note} {ext}".strip()
            else:
                fmt_note = ext

        return HistoryRecord(
            db_id=row["id"],
            video_id=extract_video_id(url),
            url=url,
            title=row["title"] or "",
            output_path=out,
            file_size=row["file_size"] or 0,
            thumbnail_url=row["thumbnail_url"] or "",
            duration=row["duration"] or 0,
            format_note=fmt_note,
            download_time=row["updated_at"],
            file_exists=True,
        )

    def _fetch_records(self, extra_where: str = "", params: tuple = ()) -> list[HistoryRecord]:
        try:
            cursor = task_db._conn.cursor()
            base_query = "SELECT * FROM tasks WHERE state IN ('completed', 'error')"
            if extra_where:
                base_query += f" AND {extra_where}"
            base_query += " ORDER BY updated_at DESC"

            cursor.execute(base_query, params)
            return [self._convert_row_to_record(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"[HistoryAdapter] Fetch failed: {e}")
            return []

    def all_records(self) -> list[HistoryRecord]:
        return self._fetch_records()

    def validated_records(self) -> list[HistoryRecord]:
        records = self.all_records()
        for r in records:
            r.file_exists = bool(r.output_path and os.path.exists(r.output_path))
        return records

    def existing_records(self) -> list[HistoryRecord]:
        return [r for r in self.validated_records() if r.file_exists]

    def grouped(self, only_existing: bool = False) -> list[HistoryGroup]:
        records = self.existing_records() if only_existing else self.validated_records()
        groups: dict[str, HistoryGroup] = {}
        for r in records:
            key = r.video_id or r.output_path
            if key not in groups:
                groups[key] = HistoryGroup(video_id=r.video_id, title=r.title, records=[])
            groups[key].records.append(r)
        return list(groups.values())

    def find_by_video_id(self, video_id: str) -> list[HistoryRecord]:
        # 虽然不是很准确（因为 video_id 是后算出来的），但兼容老 API 只需要返回 URL 包含内容的
        return self._fetch_records("url LIKE ?", (f"%{video_id}%",))

    def is_downloaded(self, url: str) -> bool:
        vid = extract_video_id(url)
        if not vid:
            return False
        records = self.find_by_video_id(vid)
        return any(bool(r.output_path and os.path.exists(r.output_path)) for r in records)

    def search(self, keyword: str) -> list[HistoryRecord]:
        return self._fetch_records("title LIKE ?", (f"%{keyword}%",))

    def remove(self, record: HistoryRecord | str) -> bool:
        try:
            db_id = record.db_id if isinstance(record, HistoryRecord) else 0
            if db_id > 0:
                task_db.delete_task(db_id)
                return True
        except Exception as e:
            logger.error(f"[HistoryAdapter] Remove failed: {e}")
        return False

    def remove_missing(self) -> int:
        records = self.all_records()
        missing = [r for r in records if not (r.output_path and os.path.exists(r.output_path))]
        count = 0
        for r in missing:
            if r.db_id > 0:
                task_db.delete_task(r.db_id)
                count += 1
        return count

    def clear(self) -> int:
        records = self.all_records()
        for r in records:
            task_db.delete_task(r.db_id)
        return len(records)

    @property
    def count(self) -> int:
        try:
            cursor = task_db._conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('completed', 'error')")
            return cursor.fetchone()[0]
        except Exception:
            return 0

    def total_size(self) -> int:
        records = self.all_records()
        return sum(r.file_size for r in records if r.output_path and os.path.exists(r.output_path))


history_service = HistoryService()
