"""Read 工具:按行读取文件内容。

已实现:
  - offset/limit 按"行"读(1-indexed 起始行 + 行数),读大文件的一段;
  - 全读时有体量卡口(默认 256KB),超限报错并提示改用 offset/limit 或 Grep;
  - 输出带行号(便于模型定位、便于配合 Edit);
  - 读后写入 read_file_state(乐观锁上锁:记录 mtime 版本号 + 内容);
  - 内容统一归一化为 \\n。


"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from ..file_state import FileState, expand_path, file_mtime_ms, read_text_lf
from ..tools import ToolContext, build_tool

# 全读体量上限(字节)。对齐 CC 的 256KB(MAX_OUTPUT_SIZE)。
MAX_READ_BYTES = 256 * 1024


def _format_with_line_numbers(lines: list[str], start_line: int) -> str:
    """每行前加右对齐 6 位行号"""
    return "\n".join(
        f"{start_line + i:>6}→{line}" for i, line in enumerate(lines)
    )


async def read(
    file_path: str,
    offset: int | None = None,
    limit: int | None = None,
) -> tuple[str, FileState, str]:
    """读取文件的 [offset, offset+limit) 行。

    返回 (带行号的文本, 记录用 FileState, 绝对路径)。offset 1-indexed;
    offset/limit 省略即全读。全读且文件超过 MAX_READ_BYTES 时抛错。
    """
    path = expand_path(file_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"文件不存在: {file_path}")
    if not os.path.isfile(path):
        raise ValueError(f"不是文件: {file_path}")

    # 全读(未指定 limit)才卡文件总大小;局部读放行。
    if limit is None and os.path.getsize(path) > MAX_READ_BYTES:
        raise ValueError(
            f"文件过大({os.path.getsize(path)} 字节 > {MAX_READ_BYTES}),无法一次全读。"
            f"请用 offset/limit 读取部分,或改用 Grep 搜索具体内容。"
        )

    content = read_text_lf(path)          # 归一化为 \n
    all_lines = content.split("\n")
    # 归一化起始行:全读时 offset 记为 1(而非 None),使 Read 记录的 offset 恒非 None,
    # 从而与 Edit/Write 写入的 offset=None 区分开 —— 去重缓存据此只认"上次 Read"的记录。
    eff_offset = offset if offset is not None else 1
    start = eff_offset - 1
    if start < 0:
        start = 0
    end = (start + limit) if limit else len(all_lines)
    slice_lines = all_lines[start:end]
    # 记录用的 content:全读=全文,局部读=切片。全读(从头且无 limit)时它 == 归一化全文,
    # 供 Edit/Write 的"内容兜底比对"使用。
    is_full = start == 0 and limit is None
    recorded_content = content if is_full else "\n".join(slice_lines)

    fs = FileState(
        content=recorded_content,
        timestamp=file_mtime_ms(path),
        offset=eff_offset,
        limit=limit,
    )
    numbered = _format_with_line_numbers(slice_lines, start + 1)
    return numbered, fs, path


class ReadInput(BaseModel):
    file_path: str = Field(description="要读取的文件路径")
    offset: int | None = Field(default=None, description="起始行号(从 1 开始);省略则从头读")
    limit: int | None = Field(default=None, description="读取的行数;省略则读到文件末尾")


async def _read_func(inp: ReadInput, ctx: ToolContext) -> str:
    path = expand_path(inp.file_path)
    # ── 去重缓存:同一文件 + 同样读取范围 + 自上次 Read 后 mtime 未变 → 不重发全文(省上下文) ──
    # 只对"上次 Read"留下的记录去重(prev.offset 非 None);Edit/Write 记录 offset=None,
    # 其内容模型并未以 Read 结果见过,故不对其去重。
    read_file_state = ctx.query_state.read_file_state
    eff_offset = inp.offset if inp.offset is not None else 1
    prev = read_file_state.get(path)
    if (
        prev is not None
        and prev.offset is not None
        and prev.offset == eff_offset
        and prev.limit == inp.limit
        and os.path.isfile(path)
        and file_mtime_ms(path) == prev.timestamp
    ):
        return "文件自上次 Read 后未改动(内容见此前的读取结果,此处不再重复输出)。"

    numbered, fs, _ = await read(inp.file_path, inp.offset, inp.limit)
    read_file_state.set(path, fs)  # 乐观锁上锁:记录版本号 + 内容
    if numbered == "":
        return "(空文件)"
    return numbered


READ_TOOL = build_tool(
    name="Read",
    description=(
        "读取文件内容,返回带行号的文本。可用 offset(起始行,1-indexed)和 "
        "limit(行数)读取大文件的某一段。用于查看某个已知文件的完整/局部内容 —— "
        "定位用 Grep/Glob,通读用本工具。"
    ),
    input_model=ReadInput,
    func=_read_func,
    is_concurrency_safe=True,  # 只读,可与其他只读工具并发
)
