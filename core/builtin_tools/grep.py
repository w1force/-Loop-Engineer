# core/builtin_tools/grep.py
"""Grep 工具: 内容搜索(ripgrep, 只读, 并发安全)。

Task 3 起 工厂无参: func 从 ctx.agent_state.cwd 取(原闭包退场)。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..tools import Tool, ToolContext

_VCS_DIRS = [".git", ".svn", ".hg", ".bzr", ".jj", ".sl"]
_DEFAULT_HEAD_LIMIT = 250

_DESCRIPTION = (
    "Search file contents with a regular expression (powered by ripgrep). "
    "Use this to find where code/text lives. Supports output_mode "
    "(files_with_matches/content/count), context lines, case-insensitive, "
    "file type filter, and pagination (head_limit/offset)."
)


class GrepIn(BaseModel):
    pattern: str
    path: str | None = None
    glob: str | None = None
    output_mode: Literal["content", "files_with_matches", "count"] = "files_with_matches"
    context_before: int | None = None   # rg -B
    context_after: int | None = None    # rg -A
    context: int | None = None          # rg -C (优先于 -B/-A)
    case_insensitive: bool = False      # rg -i
    show_line_numbers: bool = True      # rg -n (仅 content)
    type: str | None = None             # rg --type
    head_limit: int = _DEFAULT_HEAD_LIMIT   # 0 = 不限
    offset: int = 0
    multiline: bool = False             # rg -U --multiline-dotall


def _build_args(inp: GrepIn) -> list[str]:
    args = ["--hidden", "--max-columns", "500"]
    for d in _VCS_DIRS:
        args += ["--glob", f"!{d}"]
    if inp.multiline:
        args += ["-U", "--multiline-dotall"]
    if inp.case_insensitive:
        args.append("-i")
    if inp.output_mode == "files_with_matches":
        args.append("-l")
    elif inp.output_mode == "count":
        args.append("-c")
    if inp.show_line_numbers and inp.output_mode == "content":
        args.append("-n")
    if inp.output_mode == "content":
        if inp.context is not None:
            args += ["-C", str(inp.context)]
        else:
            if inp.context_before is not None:
                args += ["-B", str(inp.context_before)]
            if inp.context_after is not None:
                args += ["-A", str(inp.context_after)]
    args += ["-e", inp.pattern] if inp.pattern.startswith("-") else [inp.pattern]
    if inp.type:
        args += ["--type", inp.type]
    if inp.glob:
        args += ["--glob", inp.glob]
    return args


def _rel(path: str, base: Path) -> str:
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return path


def _paginate(items: list[str], head_limit: int, offset: int) -> tuple[list[str], bool]:
    if head_limit == 0:
        return items[offset:], False
    sliced = items[offset:offset + head_limit]
    truncated = len(items) - offset > head_limit
    return sliced, truncated


def _format_limit(truncated: bool, offset: int) -> str:
    parts = []
    if truncated:
        parts.append("more results exist")
    if offset:
        parts.append(f"offset={offset}")
    return f" [{', '.join(parts)}]" if parts else ""


def grep_tool() -> Tool:
    async def _grep(inp: GrepIn, ctx: ToolContext) -> str:
        cwd = ctx.agent_state.cwd   # ★ 从 ctx 取(原闭包)
        base = Path(inp.path) if inp.path else Path(cwd or os.getcwd())
        args = _build_args(inp)
        try:
            proc = await asyncio.create_subprocess_exec(
                "rg", *args, str(base),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "ripgrep (rg) not found. Install: brew install ripgrep (macOS) "
                "or apt install ripgrep (Debian/Ubuntu)."
            ) from e
        stdout_b, stderr_b = await proc.communicate()
        rc = proc.returncode
        if rc not in (0, 1):
            raise RuntimeError(f"rg failed (code {rc}): {stderr_b.decode().strip()}")
        lines = stdout_b.decode().splitlines() if rc == 0 else []

        if inp.output_mode == "files_with_matches":
            def _mtime(p: str) -> float:
                try:
                    return Path(p).stat().st_mtime
                except OSError:
                    return 0.0
            ordered = sorted(lines, key=lambda p: (-_mtime(p), p))  # mtime 降序, 名字 tiebreak
            sliced, truncated = _paginate(ordered, inp.head_limit, inp.offset)
            rels = [_rel(p, base) for p in sliced]
            if not rels:
                return "No files found"
            return f"Found {len(rels)} files{_format_limit(truncated, inp.offset)}\n" + "\n".join(rels)

        if inp.output_mode == "count":
            sliced, truncated = _paginate(lines, inp.head_limit, inp.offset)
            total = 0
            rendered = []
            for ln in sliced:
                rendered.append(_rel_path_before_colon(ln, base))
                idx = ln.rfind(":")
                if idx > 0:
                    n = ln[idx + 1:]
                    if n.isdigit():
                        total += int(n)
            if not rendered:
                return "No matches found"
            return ("\n".join(rendered)
                    + f"\n\nFound {total} occurrences across {len(rendered)} files"
                    + _format_limit(truncated, inp.offset))

        # content
        sliced, truncated = _paginate(lines, inp.head_limit, inp.offset)
        rendered = [_rel_path_before_colon(ln, base) for ln in sliced]
        if not rendered:
            return "No matches found"
        return "\n".join(rendered) + _format_limit(truncated, inp.offset)

    return Tool(
        name="grep",
        description=_DESCRIPTION,
        input_model=GrepIn,
        func=_grep,
        is_concurrency_safe=True,
    )


def _rel_path_before_colon(line: str, base: Path) -> str:
    """rg 输出形如 /abs/path:lineno:content 或 /abs/path:content 或 /abs/path:count。
    把首个冒号前的路径部分相对化, 其余保留。"""
    idx = line.find(":")
    if idx <= 0:
        return line
    return _rel(line[:idx], base) + line[idx:]
