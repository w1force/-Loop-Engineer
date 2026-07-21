
from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass

# 默认 LRU 容量(条目数)。对齐 CC 的 READ_FILE_STATE_CACHE_SIZE=100。
DEFAULT_CAPACITY = 100


@dataclass
class FileState:
    """一个文件"被读/写那刻"的快照,充当乐观锁的版本记录。"""

    content: str          # 记录时的内容(已归一化为 \n);全读=全文,局部读=切片
    timestamp: int        # 版本号:文件 mtime(floor 到毫秒)
    offset: int | None    # Read 的起始行(1-indexed);Edit/Write 写入时为 None(全视图)
    limit: int | None     # Read 的行数;全读/写入为 None


# ── 低层文件工具(读写 + mtime,供 Read/Edit/Write 共用)──────────────
def expand_path(path: str) -> str:
    """展开 ~ 并转绝对路径。os 操作用它;传给缓存的 key 会再被 _norm 归一化。"""
    return os.path.abspath(os.path.expanduser(path))


def file_mtime_ms(path: str) -> int:
    """文件 mtime,floor 到毫秒(整数)。

    """
    return os.stat(path).st_mtime_ns // 1_000_000


def read_text_lf(path: str) -> str:
    """读文本并把 CRLF/CR 归一化为 LF。

    Read 记录的 content、Edit 读取比对的 content 必须走同一归一化,
    否则"内容兜底比对"永远不相等
    """
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        raw = f.read()
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def write_text(path: str, content: str) -> None:
    """写文本(LF)。父目录不存在则创建。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


class FileStateCache:
    """path -> FileState 的 LRU 缓存。

    - key 统一用 _norm(归一化绝对路径),消除相对/绝对、~、大小写、分隔符差异,
      否则同一文件被当成两个 key、锁失效。
    - LRU 仅为内存保护:满了淘汰"最久没碰"的记录。被淘汰的文件会被当作"没读过",
      Edit/Write 会要求重读 —— 这是安全侧的保守失败,可接受。
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        self.capacity = capacity
        self._cache: OrderedDict[str, FileState] = OrderedDict()

    @staticmethod
    def _norm(key: str) -> str:
        return os.path.normcase(os.path.abspath(os.path.expanduser(key)))

    def get(self, key: str) -> FileState | None:
        k = self._norm(key)
        if k not in self._cache:
            return None
        self._cache.move_to_end(k)  # 用过 → 挪到末尾,标记为"最近使用"
        return self._cache[k]

    def set(self, key: str, value: FileState) -> None:
        k = self._norm(key)
        if k in self._cache:
            self._cache.move_to_end(k)
        self._cache[k] = value
        if len(self._cache) > self.capacity:
            self._cache.popitem(last=False)  # 删最前面的(最久未用)

    def has(self, key: str) -> bool:
        return self._norm(key) in self._cache

    def delete(self, key: str) -> None:
        self._cache.pop(self._norm(key), None)
