"""Write 工具:整文件写入 + 读后写乐观锁(对齐 CC 的 FileWriteTool,简化版)。

与 Edit 的分工:Edit 改一段(字符串替换),Write 换整个文件(覆盖或新建)。
两者共用同一套乐观锁语义:

  - **覆盖已存在文件**:要求"先 Read 过 + 当前 mtime 未超过记录值(或全读时内容
    一致兜底)",否则报错让模型重读 —— 防止用整份新内容覆盖掉他人/linter 的改动;
  - **新建文件**(文件不存在):跳过乐观锁,直接写;
  - 临界区(判定→写盘)之间无 await,保证读-改-写原子;
  - 写成功后更新 read_file_state(推进版本号 + 置为全视图)。

砍掉的非核心:diff 渲染、deny 规则、UNC 处理、settings/.ipynb 特判等。
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from ..file_state import (
    FileState,
    expand_path,
    file_mtime_ms,
    read_text_lf,
    write_text,
)
from ..tools import ToolContext, build_tool

# 与 Edit 共用同一文案(对齐 CC 的 FILE_UNEXPECTEDLY_MODIFIED_ERROR 语义)。
FILE_MODIFIED_ERROR = "文件在读取后被修改过(可能是用户或 linter 改的),请重新 Read 后再覆盖写入。"


class WriteInput(BaseModel):
    file_path: str = Field(description="要写入的文件路径")
    content: str = Field(description="写入的完整文件内容(整文件覆盖;文件不存在则新建)")


def _check_optimistic_lock(ctx: ToolContext, path: str, current: str) -> None:
    """覆盖已存在文件前的乐观锁校验(逻辑与 Edit 对称)。

    没读过 → 抛;当前 mtime > 记录 timestamp → 判定被改;但若"记录的内容"与当前磁盘
    完全一致,则视为未变、放行。全读/编辑后记录全文 → 相等放行;局部读记录切片 → 要求重读。
    """
    last = ctx.read_file_state.get(path)
    cur_mtime = file_mtime_ms(path)
    if last is None or cur_mtime > last.timestamp:
        content_unchanged = last is not None and current == last.content
        if not content_unchanged:
            raise ValueError(FILE_MODIFIED_ERROR)


async def _write_func(inp: WriteInput, ctx: ToolContext) -> str:
    path = expand_path(inp.file_path)
    exists = os.path.exists(path)

    # ── 临界区:以下无 await,保证判定→写盘原子 ──
    if exists:
        if not os.path.isfile(path):
            raise ValueError(f"不是文件,无法写入: {inp.file_path}")
        current = read_text_lf(path)         # 归一化后与记录内容比对
        _check_optimistic_lock(ctx, path, current)  # 覆盖走锁

    write_text(path, inp.content)            # 新建或覆盖(父目录不存在会自动建)

    # 更新锁:版本号推进到写后 mtime,置为全视图
    ctx.read_file_state.set(
        path,
        FileState(content=inp.content, timestamp=file_mtime_ms(path), offset=None, limit=None),
    )
    verb = "覆盖写入" if exists else "创建"
    return f"已{verb} {inp.file_path}"


WRITE_TOOL = build_tool(
    name="Write",
    description=(
        "整文件写入:用 content 覆盖已存在文件,或新建文件。与 Edit 的区别是 Write "
        "换整个文件、Edit 改一段。覆盖已存在文件前必须先 Read(乐观锁:防止覆盖掉他人改动);"
        "新建文件则无需先读。"
    ),
    input_model=WriteInput,
    func=_write_func,
    # 写工具:is_concurrency_safe 默认 False → executor 串行执行、独占。
)
