# core/builtin_tools/write.py
"""Write 工具: 创建/覆盖文件(写, 独占)。含陈旧检测 + unified diff 返回。

Task 3 起 工厂无参: func 从 ctx.agent_state.file_read_state 取(原闭包退场)。
"""
from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel

from ..tools import ToolContext, build_tool

_DESCRIPTION = (
    "Write a file to the local filesystem. Overwrites existing files. "
    "For files you have read, refuses if the file changed on disk since your last read "
    "(re-read first). Creates parent directories as needed."
)


class WriteIn(BaseModel):
    file_path: str
    content: str


async def _write(inp: WriteIn, ctx: ToolContext) -> str:
    read_state = ctx.agent_state.file_read_state   # ★ 从 ctx 取(原闭包)
    path = Path(inp.file_path)
    exists = path.exists()

    # 陈旧检测: 读过且读后被外部改了 → 拒绝(仅存在于磁盘上的文件才有 mtime 可比)
    if exists:
        disk_mtime = path.stat().st_mtime
        if read_state.is_stale(str(path), disk_mtime):
            raise PermissionError(
                "File has been modified since read. Read it again before writing.")

    # mkdir 父目录
    path.parent.mkdir(parents=True, exist_ok=True)

    old = path.read_text(encoding="utf-8") if exists else None

    # 写(LF, 不重写行尾)
    path.write_text(inp.content, encoding="utf-8", newline="\n")

    new_mtime = path.stat().st_mtime
    read_state.set(str(path), inp.content, new_mtime, 0, None)

    if old is None:
        return f"File created successfully at: {inp.file_path}"

    diff = "\n".join(difflib.unified_diff(
        old.splitlines(), inp.content.splitlines(),
        fromfile=inp.file_path, tofile=inp.file_path, lineterm=""))
    if diff:
        return f"The file {inp.file_path} has been updated.\n{diff}"
    return f"The file {inp.file_path} has been updated (no content change)."

WRITE_TOOL = build_tool(
    name="Write",
    description=_DESCRIPTION,
    input_model=WriteIn,
    func=_write,
    is_concurrency_safe=False,
)
