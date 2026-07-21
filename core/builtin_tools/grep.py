"""Grep 工具:用正则搜索文件内容(对齐 CC 的 GrepTool.ts,简化版)。

与 glob 不同,它搜索文件内部。构建在同一个 ripgrep 引擎上。

保留的核心:三种输出模式、glob 文件过滤、大小写不敏感、.git 排除。
砍掉的非核心内容:-A/-B/-C 上下文、head_limit 分页、mtime 排序、multiline、
--type、ignore 规则注入、路径相对化。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..tools import ToolContext, build_tool
from .ripgrep import rip_grep

OutputMode = Literal["content", "files_with_matches", "count"]


async def grep(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    output_mode: OutputMode = "files_with_matches",
    case_insensitive: bool = False,
) -> list[str]:
    """在文件内容中搜索正则 ``pattern``,返回 rg 输出行。"""
    args = ["--hidden", "--glob", "!.git"]  # 含隐藏文件,但排除 .git 噪音

    if output_mode == "files_with_matches":
        args.append("-l")            # 只列命中文件
    elif output_mode == "count":
        args.append("-c")            # 每文件计数
    else:  # content
        args.append("-n")            # 显示行号

    if case_insensitive:
        args.append("-i")
    if glob:
        args.extend(["--glob", glob])

    # 以 '-' 开头的 pattern 用 -e 传,避免被当成 rg 参数
    if pattern.startswith("-"):
        args.extend(["-e", pattern])
    else:
        args.append(pattern)

    return await rip_grep(args, path)


class GrepInput(BaseModel):
    pattern: str = Field(description="要在文件内容中搜索的正则表达式(ripgrep 语法)")
    path: str | None = Field(default=None, description="搜索的文件或目录,省略则用当前工作目录")
    glob: str | None = Field(default=None, description='按文件名过滤,如 "*.py" 或 "**/*.ts"')
    output_mode: OutputMode = Field(
        default="files_with_matches",
        description='输出模式:content(匹配行) / files_with_matches(命中文件,默认) / count(计数)',
    )
    case_insensitive: bool = Field(default=False, description="大小写不敏感搜索")


async def _grep_func(inp: GrepInput, ctx: ToolContext) -> str:
    lines = await grep(
        pattern=inp.pattern,
        path=inp.path or ctx.agent_state.cwd,
        glob=inp.glob,
        output_mode=inp.output_mode,
        case_insensitive=inp.case_insensitive,
    )
    if not lines:
        return "No matches found"
    if inp.output_mode == "files_with_matches":
        return f"找到 {len(lines)} 个文件\n" + "\n".join(lines)
    return "\n".join(lines)


GREP_TOOL = build_tool(
    name="Grep",
    description=(
        "基于 ripgrep 的内容搜索工具。支持完整正则语法;可用 glob 参数过滤文件;"
        "三种输出模式:content / files_with_matches(默认) / count。用于搜索文件内容。"
    ),
    input_model=GrepInput,
    func=_grep_func,
    is_concurrency_safe=True,  # 只读,可并发
)
