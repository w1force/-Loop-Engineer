# core/builtin_tools/read.py
"""Read 工具: 读文本文件(按行 + 行号, 只读, 并发安全)。支持去重 + 陈旧记录。"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ..tools import Tool, ToolContext
from .readstate import FileReadState

MAX_READ_BYTES = 256_000

_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".zip", ".gz", ".tar", ".bz2", ".7z", ".rar",
    ".pdf", ".exe", ".dll", ".so", ".dylib", ".class", ".jar",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac",
    ".pyc", ".pyo", ".o", ".a", ".woff", ".woff2", ".ttf",
}

_BLOCKED_DEVICES = {
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr", "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
}

_DESCRIPTION = (
    "Read a text file from the local filesystem. Output has line numbers. "
    "Supports offset/limit for large files. Cannot read binary files."
)


class ReadIn(BaseModel):
    file_path: str
    offset: int = 1        # 1-indexed
    limit: int | None = None


def _is_blocked_device(path: str) -> bool:
    if path in _BLOCKED_DEVICES:
        return True
    return path.startswith("/proc/") and (
        path.endswith("/fd/0") or path.endswith("/fd/1") or path.endswith("/fd/2"))


def _add_line_numbers(lines: list[str], start: int, total: int) -> str:
    width = max(2, len(str(total)))
    out = []
    for i, ln in enumerate(lines):
        out.append(f"{start + i:>{width}}\t{ln}")
    return "\n".join(out)


def read_tool(read_state: FileReadState, cwd: str | None = None) -> Tool:
    async def _read(inp: ReadIn, ctx: ToolContext) -> str:
        path = Path(inp.file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {inp.file_path}")
        if path.suffix.lower() in _BINARY_EXTS:
            raise ValueError(
                f"Cannot read binary file ({path.suffix}). Use a different tool.")
        if _is_blocked_device(str(path)):
            raise ValueError(
                f"Cannot read '{inp.file_path}': device file would block or produce infinite output.")

        disk_mtime = path.stat().st_mtime

        # 去重
        if read_state.is_unchanged(str(path), inp.offset, inp.limit, disk_mtime):
            return "File unchanged"

        all_lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
        if all_lines and all_lines[-1] == "":
            all_lines = all_lines[:-1]   # 末尾空行(由 trailing \n 产生)不计
        total = len(all_lines)

        if total == 0:
            read_state.set(str(path), "", disk_mtime, inp.offset, inp.limit)
            return "<File is empty>"

        start_idx = inp.offset - 1
        if start_idx >= total:
            # 越界也算"尝试读过": 记 read_state, 否则后续 write 会因无记录漏判陈旧(I-1)
            read_state.set(str(path), "", disk_mtime, inp.offset, inp.limit)
            return f"<File has {total} line(s); offset {inp.offset} out of range.>"

        end_idx = total if inp.limit is None else min(start_idx + inp.limit, total)
        selected = all_lines[start_idx:end_idx]

        # 字节上限: 累积截断
        kept: list[str] = []
        size = 0
        for ln in selected:
            if size + len(ln) > MAX_READ_BYTES:
                break
            kept.append(ln)
            size += len(ln) + 1
        truncated_bytes = len(kept) < len(selected)

        read_state.set(str(path), "\n".join(kept), disk_mtime, inp.offset, inp.limit)
        out = _add_line_numbers(kept, inp.offset, total)
        if truncated_bytes:
            out += f"\n<Read truncated at {MAX_READ_BYTES} bytes; use offset/limit for more.>"
        return out

    return Tool(
        name="read",
        description=_DESCRIPTION,
        input_model=ReadIn,
        func=_read,
        is_concurrency_safe=True,
    )
