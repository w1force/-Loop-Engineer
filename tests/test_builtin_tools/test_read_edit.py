"""Read / Edit 工具 + 乐观锁测试。

用 tmp_path 建真实文件,覆盖:Read 的行/切片/体量卡口/写锁,Edit 的匹配守卫、
新建、以及乐观锁(必须先读、读后被改则拒、内容兜底),并经统一入口 executor 跑通。
"""
import asyncio
import os
import time

import pytest

from core.builtin_tools import EDIT_TOOL, READ_TOOL
from core.builtin_tools.edit import FILE_MODIFIED_ERROR, EditInput, _edit_func
from core.builtin_tools.read import MAX_READ_BYTES, ReadInput, _read_func, read
from core.file_state import FileStateCache, file_mtime_ms
from core.registry import get_all_base_tools
from core.tool_executor import make_executor
from core.tools import ToolContext, default_can_use_tool
from core.types import ToolResultBlock, ToolUseBlock
from telemetry.tracer import NoopTracer


def _ctx(cache: FileStateCache | None = None) -> ToolContext:
    return ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        read_file_state=cache or FileStateCache(),
    )


def _bump_mtime(path: str, secs: int = 2) -> None:
    """把文件 mtime 往后推,模拟"读之后被外部改动"(避免同毫秒判等)。"""
    t = time.time() + secs
    os.utime(path, (t, t))


# ── 注册表 ──────────────────────────────────────────────
def test_registry_includes_read_and_edit():
    names = {t.name for t in get_all_base_tools()}
    assert {"Glob", "Grep", "Read", "Edit"} <= names


# ── Read ────────────────────────────────────────────────
async def test_read_returns_numbered_lines_and_locks(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    ctx = _ctx()
    out = await _read_func(ReadInput(file_path=str(f)), ctx)
    assert "1→line1" in out and "2→line2" in out
    # 读后已上锁:记录了该文件的 FileState
    assert ctx.read_file_state.get(str(f)) is not None


async def test_read_offset_limit(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("\n".join(f"L{i}" for i in range(1, 11)), encoding="utf-8")
    numbered, fs, _ = await read(str(f), offset=3, limit=2)
    assert "3→L3" in numbered and "4→L4" in numbered
    assert "5→L5" not in numbered
    assert fs.offset == 3 and fs.limit == 2  # 局部读记录了范围


async def test_read_full_read_byte_cap(tmp_path):
    f = tmp_path / "big.txt"
    f.write_bytes(b"x" * (MAX_READ_BYTES + 1))
    with pytest.raises(ValueError, match="文件过大"):
        await read(str(f))  # 全读超限报错
    # 但带 limit 的局部读放行
    out, _, _ = await read(str(f), offset=1, limit=1)
    assert out  # 不抛


# ── Edit:基础替换 ───────────────────────────────────────
async def test_edit_after_read_succeeds(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)  # 先读(上锁)
    msg = await _edit_func(
        EditInput(file_path=str(f), old_string="return 1", new_string="return 2"), ctx
    )
    assert "已编辑" in msg
    assert "return 2" in f.read_text(encoding="utf-8")


async def test_edit_replace_all(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("a\na\na\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)
    # 多处匹配 + 未开 replace_all → 报错
    with pytest.raises(ValueError, match="replace_all"):
        await _edit_func(EditInput(file_path=str(f), old_string="a", new_string="b"), ctx)
    # 开 replace_all → 全替换
    await _edit_func(
        EditInput(file_path=str(f), old_string="a", new_string="b", replace_all=True), ctx
    )
    assert f.read_text(encoding="utf-8") == "b\nb\nb\n"


async def test_edit_string_not_found(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("hello\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)
    with pytest.raises(ValueError, match="未找到"):
        await _edit_func(EditInput(file_path=str(f), old_string="世界", new_string="x"), ctx)


async def test_edit_old_equals_new(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)
    with pytest.raises(ValueError, match="完全相同"):
        await _edit_func(EditInput(file_path=str(f), old_string="x", new_string="x"), ctx)


async def test_edit_create_new_file_with_empty_old(tmp_path):
    f = tmp_path / "new.txt"
    ctx = _ctx()
    msg = await _edit_func(
        EditInput(file_path=str(f), old_string="", new_string="hello\n"), ctx
    )
    assert "已创建" in msg
    assert f.read_text(encoding="utf-8") == "hello\n"


# ── Edit:乐观锁 ─────────────────────────────────────────
async def test_edit_without_read_is_rejected(tmp_path):
    f = tmp_path / "g.py"
    f.write_text("x = 1\n", encoding="utf-8")
    ctx = _ctx()  # 没读过
    with pytest.raises(ValueError, match="重新 Read"):
        await _edit_func(EditInput(file_path=str(f), old_string="x = 1", new_string="x = 2"), ctx)


async def test_edit_rejected_when_modified_since_read(tmp_path):
    f = tmp_path / "h.py"
    f.write_text("x = 1\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)  # 读(记录当时 mtime)
    _bump_mtime(f)  # 模拟外部改动:mtime 前进
    f.write_text("x = 999  # 别人改的\n", encoding="utf-8")
    _bump_mtime(f)
    with pytest.raises(ValueError, match="重新 Read"):
        await _edit_func(EditInput(file_path=str(f), old_string="x = 1", new_string="x = 2"), ctx)


async def test_edit_content_fallback_allows_when_mtime_bumped_but_content_same(tmp_path):
    # mtime 变了但内容没变(全读)→ 内容兜底放行
    f = tmp_path / "i.py"
    f.write_text("x = 1\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)  # 全读
    _bump_mtime(f)  # 只动 mtime,不改内容
    msg = await _edit_func(
        EditInput(file_path=str(f), old_string="x = 1", new_string="x = 2"), ctx
    )
    assert "已编辑" in msg


async def test_edit_then_edit_again_no_reread_needed(tmp_path):
    # 编辑成功会更新锁 → 紧接着再编辑无需重读
    f = tmp_path / "j.py"
    f.write_text("a = 1\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)
    await _edit_func(EditInput(file_path=str(f), old_string="a = 1", new_string="a = 2"), ctx)
    _bump_mtime(f)  # 即便 mtime 因写入前进,锁已被 Edit 更新到写后状态
    msg = await _edit_func(EditInput(file_path=str(f), old_string="a = 2", new_string="a = 3"), ctx)
    assert "已编辑" in msg
    assert "a = 3" in f.read_text(encoding="utf-8")


# ── 端到端:经统一入口 executor ─────────────────────────
async def test_read_edit_roundtrip_via_executor(tmp_path):
    f = tmp_path / "k.py"
    f.write_text("v = 10\n", encoding="utf-8")
    cache = FileStateCache()
    ctx = _ctx(cache)
    ex = make_executor("batch", [READ_TOOL, EDIT_TOOL], default_can_use_tool, NoopTracer(), ctx)
    # 先 Read(经统一入口,写入同一 read_file_state)
    ex.add_tool(ToolUseBlock(id="r1", name="Read", input={"file_path": str(f)}))
    r1 = await ex.get_results()
    assert not r1[0].is_error and "1→v = 10" in _content(r1[0])
    # 再 Edit(乐观锁校验通过 —— 因为共享同一 ctx.read_file_state)
    ex2 = make_executor("batch", [READ_TOOL, EDIT_TOOL], default_can_use_tool, NoopTracer(), ctx)
    ex2.add_tool(
        ToolUseBlock(id="e1", name="Edit", input={"file_path": str(f), "old_string": "v = 10", "new_string": "v = 20"})
    )
    r2 = await ex2.get_results()
    assert not r2[0].is_error
    assert "v = 20" in f.read_text(encoding="utf-8")


def _content(block: ToolResultBlock) -> str:
    return block.content if isinstance(block.content, str) else str(block.content)


# ── Edit:删除操作修剪尾随换行 ───────────────────────────
async def test_edit_deletion_strips_trailing_newline(tmp_path):
    # 文件 "bar\nfoo",删除 "bar" → 应得 "foo"(不留空行),而非 "\nfoo"
    f = tmp_path / "del.txt"
    f.write_text("bar\nfoo", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)
    await _edit_func(EditInput(file_path=str(f), old_string="bar", new_string=""), ctx)
    assert f.read_text(encoding="utf-8") == "foo"


async def test_edit_deletion_without_trailing_newline_unaffected(tmp_path):
    # old 后面没有换行时,不应误删别处
    f = tmp_path / "del2.txt"
    f.write_text("a=1; b=2", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)
    await _edit_func(EditInput(file_path=str(f), old_string="b=2", new_string=""), ctx)
    assert f.read_text(encoding="utf-8") == "a=1; "


# ── Read:去重缓存 ───────────────────────────────────────
async def test_read_dedup_when_unchanged(tmp_path):
    f = tmp_path / "dd.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    ctx = _ctx()
    first = await _read_func(ReadInput(file_path=str(f)), ctx)
    assert "1→a" in first
    second = await _read_func(ReadInput(file_path=str(f)), ctx)
    assert "未改动" in second  # 去重命中:不重发内容


async def test_read_dedup_invalidated_after_modification(tmp_path):
    f = tmp_path / "de.txt"
    f.write_text("a\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)
    _bump_mtime(f)
    f.write_text("a\nb\n", encoding="utf-8")
    _bump_mtime(f)
    out = await _read_func(ReadInput(file_path=str(f)), ctx)
    assert "1→a" in out and "2→b" in out  # 变了 → 重新返回全文,不去重


async def test_read_no_dedup_against_edit_entry(tmp_path):
    # Edit 写入的记录(offset=None)不应触发 Read 去重
    f = tmp_path / "dg.txt"
    f.write_text("x\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)  # Read 记录 offset=1
    await _edit_func(EditInput(file_path=str(f), old_string="x", new_string="y"), ctx)  # Edit 记录 offset=None
    out = await _read_func(ReadInput(file_path=str(f)), ctx)  # 再读:不去重,返回新全文
    assert "1→y" in out


async def test_read_dedup_different_range_not_hit(tmp_path):
    # 读取范围不同 → 不去重
    f = tmp_path / "dh.txt"
    f.write_text("\n".join(f"L{i}" for i in range(1, 6)), encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)  # 全读
    out = await _read_func(ReadInput(file_path=str(f), offset=2, limit=2), ctx)  # 换范围
    assert "2→L2" in out and "未改动" not in out
