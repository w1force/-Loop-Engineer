"""builtin 工具集(read/write/glob/grep/load_skill)。全部无状态:func 从 ctx.agent_state 取。"""
from __future__ import annotations

from ..tools import Tool
from .glob import glob_tool
from .grep import grep_tool
from .load_skill import load_skill_tool
from .read import read_tool
from .write import write_tool

__all__ = ["builtin_tools"]


def builtin_tools() -> list[Tool]:
    """产出 5 个 builtin Tool(无参;func 从 ctx.agent_state 取运行时数据)。"""
    return [glob_tool(), grep_tool(), read_tool(), write_tool(), load_skill_tool]
