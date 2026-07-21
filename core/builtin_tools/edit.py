"""Edit 工具:字符串替换 + 读后写乐观锁。

已实现:
  - old_string 纯字面(includes)匹配;0 次/多次匹配的守卫;replace_all;
  - old_string == new_string 拒绝;空 old_string 当"新建文件";
  - **乐观锁**:编辑已存在文件前,要求"先 Read 过 + 当前 mtime 未超过记录值
    (或全读时内容一致兜底)",否则报错让模型重读;
  - 临界区(读盘→判定→写盘)之间无 await,避免并发编辑交错;
  - 编辑成功后更新 read_file_state(推进版本号 + 置为全视图)。

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

# 乐观锁失败文案(对齐 CC 的 FILE_UNEXPECTEDLY_MODIFIED_ERROR 语义)。
FILE_MODIFIED_ERROR = "文件在读取后被修改过(可能是用户或 linter 改的),请重新 Read 后再编辑。"


class EditInput(BaseModel):
    file_path: str = Field(description="要编辑的文件路径")
    old_string: str = Field(description="要被替换的原文(字面匹配)。为空则表示新建文件")
    new_string: str = Field(description="替换成的新内容")
    replace_all: bool = Field(default=False, description="是否替换所有匹配(默认只替换唯一一处)")


def _check_optimistic_lock(ctx: ToolContext, path: str, current: str) -> None:
    """乐观锁:确认文件自上次 Read 之后没被改动过,否则抛错。

    规则:没读过 → 抛;当前 mtime > 记录 timestamp → 判定被改;但若"记录的内容"与
    当前磁盘完全一致,则视为未变、放行(兜底 mtime 假阳性,如云同步/杀软 touch)。
    全读/编辑后记录的是全文 → 相等即放行;局部读记录的是切片,与全文不等 → 要求重读。
    """
    last = ctx.read_file_state.get(path)
    cur_mtime = file_mtime_ms(path)
    if last is None or cur_mtime > last.timestamp:
        content_unchanged = last is not None and current == last.content
        if not content_unchanged:
            raise ValueError(FILE_MODIFIED_ERROR)


async def _edit_func(inp: EditInput, ctx: ToolContext) -> str:
    path = expand_path(inp.file_path)
    old, new = inp.old_string, inp.new_string

    if old == new:
        raise ValueError("old_string 与 new_string 完全相同,没有要修改的内容。")

    exists = os.path.exists(path)

    # ── 新建文件:空 old_string ──
    if not exists:
        if old == "":
            write_text(path, new)
            ctx.read_file_state.set(
                path,
                FileState(content=new, timestamp=file_mtime_ms(path), offset=None, limit=None),
            )
            return f"已创建文件 {inp.file_path}"
        raise ValueError(f"文件不存在: {inp.file_path}(如需新建,请把 old_string 留空)")

    # ── 编辑已存在文件:临界区(以下无 await,保证读-改-写原子)──
    current = read_text_lf(path)
    _check_optimistic_lock(ctx, path, current)

    if old == "":
        if current != "":
            raise ValueError("old_string 为空但文件已存在且非空,无法当作新建。")
        updated = new  # 空文件写入内容
    else:
        count = current.count(old)
        if count == 0:
            raise ValueError(f"要替换的字符串在文件中未找到:\n{old}")
        if count > 1 and not inp.replace_all:
            raise ValueError(
                f"找到 {count} 处匹配,但 replace_all=false。"
                f"请设 replace_all=true 全部替换,或提供更多上下文使匹配唯一。"
            )
        # 删除操作(new 为空):若 old 不以 \n 结尾但其后紧跟换行,连尾随换行一起删,
        # 避免留下空行(对齐 CC applyEditToFile)。
        search = old
        if new == "" and not old.endswith("\n") and (old + "\n") in current:
            search = old + "\n"
        updated = current.replace(search, new) if inp.replace_all else current.replace(search, new, 1)

    write_text(path, updated)
    # 更新锁:推进版本号到写后 mtime,置为全视图(后续 Edit 可走内容兜底)
    ctx.read_file_state.set(
        path,
        FileState(content=updated, timestamp=file_mtime_ms(path), offset=None, limit=None),
    )
    n = (current.count(old) if inp.replace_all else 1) if old else 1
    return f"已编辑 {inp.file_path}({n} 处替换)"


EDIT_TOOL = build_tool(
    name="Edit",
    description=(
        "对文件做字符串替换。old_string 为字面匹配的原文,new_string 为替换内容;"
        "默认要求匹配唯一,多处匹配需设 replace_all=true。old_string 留空表示新建文件。"
        "编辑已存在文件前必须先 Read(乐观锁:防止基于旧内容盲改或覆盖他人改动)。"
    ),
    input_model=EditInput,
    func=_edit_func,
    # 写工具:is_concurrency_safe 默认 False → executor 串行执行、独占。
)
