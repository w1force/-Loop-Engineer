"""agent 级文件读状态: read 记录 mtime, write 查陈旧。跨轮持久(不随 ToolContext 重建)。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReadRecord:
    content: str
    mtime: float
    offset: int
    limit: int | None


class FileReadState:
    def __init__(self) -> None:
        self._records: dict[str, ReadRecord] = {}

    def set(self, path: str, content: str, mtime: float,
            offset: int, limit: int | None) -> None:
        self._records[path] = ReadRecord(content, mtime, offset, limit)

    def get(self, path: str) -> ReadRecord | None:
        return self._records.get(path)

    def is_unchanged(self, path: str, offset: int,
                     limit: int | None, disk_mtime: float) -> bool:
        """read 去重: 同 (path, offset, limit) 且 mtime 未变 → True。"""
        rec = self._records.get(path)
        return (rec is not None and rec.offset == offset
                and rec.limit == limit and rec.mtime == disk_mtime)

    def is_stale(self, path: str, disk_mtime: float) -> bool:
        """write 陈旧: 读过且读后被外部改了(disk mtime > 记录) → True。没读过 → False。"""
        rec = self._records.get(path)
        return rec is not None and disk_mtime > rec.mtime
