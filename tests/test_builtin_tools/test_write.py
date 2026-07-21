"""Write 工具测试:新建放行、覆盖走乐观锁,并经统一入口 executor 跑通。"""
import asyncio
import os
import time

import pytest

from core.builtin_tools import READ_TOOL, WRITE_TOOL
from core.builtin_tools.read import ReadInput, _read_func
from core.builtin_tools.write import WriteInput, _write_func
from core.file_state import FileStateCache
from core.registry import get_all_base_tools
from core.tool_executor import make_executor
from core.tools import ToolContext, default_can_use_tool
from core.types import ToolUseBlock
from telemetry.tracer import NoopTracer


def _ctx(cache: FileStateCache | None = None) -> ToolContext:
    return ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        read_file_state=cache or FileStateCache(),
    )


def _bump_mtime(path: str, secs: int = 2) -> None:
    t = time.time() + secs
    os.utime(path, (t, t))


def test_registry_includes_write():
    assert "Write" in {t.name for t in get_all_base_tools()}


# ── 新建:无需先 Read ────────────────────────────────────
async def test_write_new_file_without_read(tmp_path):
    f = tmp_path / "new.py"
    ctx = _ctx()
    msg = await _write_func(WriteInput(file_path=str(f), content="x = 1\n"), ctx)
    assert "创建" in msg
    assert f.read_text(encoding="utf-8") == "x = 1\n"
    # 写后已上锁:后续可直接再覆盖(无需重读)
    assert ctx.read_file_state.get(str(f)) is not None


async def test_write_creates_parent_dirs(tmp_path):
    f = tmp_path / "sub" / "dir" / "a.txt"
    ctx = _ctx()
    await _write_func(WriteInput(file_path=str(f), content="hi"), ctx)
    assert f.read_text(encoding="utf-8") == "hi"


# ── 覆盖:走乐观锁 ───────────────────────────────────────
async def test_overwrite_existing_without_read_is_rejected(tmp_path):
    f = tmp_path / "b.py"
    f.write_text("old\n", encoding="utf-8")
    ctx = _ctx()  # 没读过已存在文件
    with pytest.raises(ValueError, match="重新 Read"):
        await _write_func(WriteInput(file_path=str(f), content="new\n"), ctx)


async def test_overwrite_after_read_succeeds(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("old\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)  # 先读上锁
    msg = await _write_func(WriteInput(file_path=str(f), content="new\n"), ctx)
    assert "覆盖写入" in msg
    assert f.read_text(encoding="utf-8") == "new\n"


async def test_overwrite_rejected_when_modified_since_read(tmp_path):
    f = tmp_path / "d.py"
    f.write_text("v1\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)
    _bump_mtime(f)
    f.write_text("v2 别人改的\n", encoding="utf-8")
    _bump_mtime(f)
    with pytest.raises(ValueError, match="重新 Read"):
        await _write_func(WriteInput(file_path=str(f), content="v3\n"), ctx)


async def test_overwrite_content_fallback_when_mtime_bumped_but_same(tmp_path):
    # mtime 变了但内容没变(全读)→ 内容兜底放行
    f = tmp_path / "e.py"
    f.write_text("same\n", encoding="utf-8")
    ctx = _ctx()
    await _read_func(ReadInput(file_path=str(f)), ctx)
    _bump_mtime(f)  # 仅动 mtime
    msg = await _write_func(WriteInput(file_path=str(f), content="changed\n"), ctx)
    assert "覆盖写入" in msg


async def test_write_then_write_again_no_reread(tmp_path):
    # 写成功更新锁 → 紧接着再覆盖无需重读
    f = tmp_path / "g.py"
    ctx = _ctx()
    await _write_func(WriteInput(file_path=str(f), content="a\n"), ctx)  # 新建
    _bump_mtime(f)
    msg = await _write_func(WriteInput(file_path=str(f), content="b\n"), ctx)  # 覆盖
    assert "覆盖写入" in msg
    assert f.read_text(encoding="utf-8") == "b\n"


# ── 端到端:经统一入口 executor ─────────────────────────
async def test_write_roundtrip_via_executor(tmp_path):
    f = tmp_path / "h.py"
    ctx = _ctx()
    ex = make_executor("batch", [READ_TOOL, WRITE_TOOL], default_can_use_tool, NoopTracer(), ctx)
    # 新建(无需先读)
    ex.add_tool(ToolUseBlock(id="w1", name="Write", input={"file_path": str(f), "content": "n = 1\n"}))
    r1 = await ex.get_results()
    assert not r1[0].is_error
    assert f.read_text(encoding="utf-8") == "n = 1\n"
    # 覆盖:因新建时已上锁,直接覆盖能过乐观锁
    ex2 = make_executor("batch", [READ_TOOL, WRITE_TOOL], default_can_use_tool, NoopTracer(), ctx)
    ex2.add_tool(ToolUseBlock(id="w2", name="Write", input={"file_path": str(f), "content": "n = 2\n"}))
    r2 = await ex2.get_results()
    assert not r2[0].is_error
    assert f.read_text(encoding="utf-8") == "n = 2\n"
