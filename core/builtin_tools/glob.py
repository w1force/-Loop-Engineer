"""Glob 工具:按文件名模式查找文件(对齐 CC 的 src/utils/glob.ts,简化版)。

不是内容搜索:驱动 ``rg --files --glob <pattern>``,让 ripgrep 枚举文件路径
(从不读内容),按修改时间排序。

砍掉的非核心内容:绝对路径 base-dir 提取、ignore 规则注入、插件缓存排除、
路径相对化。这里搜索目录直接用 ``path``,返回 rg 给的相对路径。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..tools import ToolContext, build_tool
from .ripgrep import rip_grep

DEFAULT_LIMIT = 100


DEFAULT_EXCLUDE_DIRS = (
    ".venv", "venv", ".venv-vm", "node_modules", ".git",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build",
)


async def glob(pattern: str, path: str = ".", limit: int = DEFAULT_LIMIT) -> dict:
    """列出匹配 ``pattern`` 的文件,按 mtime 排序。返回 {files, truncated}。"""
    args = [
        "--files",           # 只列文件,不搜内容 —— glob 区别于 grep 的本质
        "--glob", pattern,   # 按模式过滤文件名
        "--sort=modified",   # 按修改时间排序
        "--no-ignore",       # 不理会 .gitignore(对齐 CC 默认)
        "--hidden",          # 含隐藏文件(对齐 CC 默认)
    ]
    # 排除规则放在 include 模式之后:rg 的 --glob 按顺序应用、后者优先,
    # 所以 !dir 能盖过前面的 include,把噪音目录整棵剪掉。
    for d in DEFAULT_EXCLUDE_DIRS:
        args.extend(["--glob", f"!{d}"])

    paths = await rip_grep(args, path)
    truncated = len(paths) > limit
    return {"files": paths[:limit], "truncated": truncated}


class GlobInput(BaseModel):
    pattern: str = Field(description='用于匹配文件的 glob 模式,如 "**/*.py" 或 "src/**/*.ts"')
    path: str | None = Field(default=None, description="搜索目录,省略则用当前工作目录")


async def _glob_func(inp: GlobInput, ctx: ToolContext) -> str:
    res = await glob(inp.pattern, inp.path or ".")
    if not res["files"]:
        return "No files found"
    lines = list(res["files"])
    if res["truncated"]:
        lines.append("(结果已截断,请使用更具体的路径或模式。)")
    return "\n".join(lines)


GLOB_TOOL = build_tool(
    name="Glob",
    description=(
        "快速的文件名模式匹配工具。支持 glob 模式(如 **/*.py、src/**/*.ts),"
        "返回按修改时间排序的文件路径。用于按文件名查找文件,不搜索内容。"
    ),
    # inputschema？
    input_model=GlobInput,
    # 具体执行函数？
    func=_glob_func,
    is_concurrency_safe=True,  # 只读,可与其他只读工具并发
)
