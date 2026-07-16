# core/builtin_tools/glob.py
"""Glob 工具: 按文件名 pattern 匹配(只读, 并发安全)。"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

from ..tools import Tool, ToolContext

_GLOB_LIMIT = 100

_DESCRIPTION = (
    "Fast file pattern matching tool. Use this to find files by name pattern. "
    "Always use this tool first when you need to find files by name. "
    "Pattern supports ** for recursive matching."
)


class GlobIn(BaseModel):
    pattern: str
    path: str | None = None


def glob_tool(cwd: str | None = None) -> Tool:
    async def _glob(inp: GlobIn, ctx: ToolContext) -> str:
        base = Path(inp.path) if inp.path else Path(cwd or os.getcwd())
        files: list[str] = []
        for p in base.glob(inp.pattern):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(base)
            except ValueError:
                rel = p  # base 外, 用绝对路径
            if ".git" in rel.parts:
                continue
            files.append(str(rel))
        files.sort()
        truncated = len(files) > _GLOB_LIMIT
        head = files[:_GLOB_LIMIT]
        if not head:
            return "No files found"
        out = "\n".join(head)
        if truncated:
            out += "\n(Results are truncated. Consider a more specific pattern.)"
        return out

    return Tool(
        name="glob",
        description=_DESCRIPTION,
        input_model=GlobIn,
        func=_glob,
        is_concurrency_safe=True,
    )
