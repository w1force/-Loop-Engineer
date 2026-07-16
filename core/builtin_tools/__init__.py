"""builtin 工具集 (read/write/glob/grep)。"""
from __future__ import annotations

from ..tools import Tool
from .glob import glob_tool
from .grep import grep_tool
from .read import read_tool
from .readstate import FileReadState
from .write import write_tool

__all__ = ["FileReadState", "builtin_tools"]


def builtin_tools(read_state: FileReadState, *, cwd: str | None = None) -> list[Tool]:
    """产出 4 个 builtin Tool。read/write 共享 read_state(陈旧检测)。cwd 默认 os.getcwd()。"""
    return [
        glob_tool(cwd),
        grep_tool(cwd),
        read_tool(read_state, cwd),
        write_tool(read_state, cwd),
    ]
