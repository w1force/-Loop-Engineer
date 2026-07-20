# tests/test_builtin_tools/test_read.py
import asyncio
import os

from core.builtin_tools.read import ReadIn, read_tool
from core.tools import ToolContext
from core.types import AgentState, FileReadState
from telemetry.tracer import NoopTracer


def _ctx(agent_state: AgentState) -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=agent_state)


async def test_read_adds_line_numbers(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("line1\nline2\nline3\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await read_tool().func(ReadIn(file_path=str(f)), _ctx(agent_state))
    assert isinstance(result, str)
    assert "1" in result and "line1" in result
    assert "2" in result and "line2" in result


async def test_read_offset_limit(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("l1\nl2\nl3\nl4\nl5\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await read_tool().func(
        ReadIn(file_path=str(f), offset=2, limit=2), _ctx(agent_state))
    assert isinstance(result, str)
    assert "l2" in result and "l3" in result
    assert "l1" not in result and "l4" not in result


async def test_read_dedup_unchanged(tmp_path):
    """同一 agent_state(read_state 共享):首次读记录,二次同 range+mtime → unchanged。"""
    f = tmp_path / "a.txt"; f.write_text("hello\n")
    agent_state = AgentState(cwd=str(tmp_path))
    tool = read_tool()
    await tool.func(ReadIn(file_path=str(f)), _ctx(agent_state))   # 首次读, 记录
    result = await tool.func(ReadIn(file_path=str(f)), _ctx(agent_state))  # 同 range, mtime 未变
    assert isinstance(result, str)
    assert result == "File unchanged"


async def test_read_after_external_change_re_reads(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("hello\n")
    agent_state = AgentState(cwd=str(tmp_path))
    tool = read_tool()
    await tool.func(ReadIn(file_path=str(f)), _ctx(agent_state))
    os.utime(str(f), (os.path.getmtime(str(f)) + 100, os.path.getmtime(str(f)) + 100))
    result = await tool.func(ReadIn(file_path=str(f)), _ctx(agent_state))
    assert isinstance(result, str)
    assert result != "File unchanged"
    assert "hello" in result


async def test_read_binary_rejected(tmp_path):
    f = tmp_path / "a.png"; f.write_bytes(b"\x89PNG\r\n")
    agent_state = AgentState(cwd=str(tmp_path))
    import pytest
    with pytest.raises(Exception):
        await read_tool().func(ReadIn(file_path=str(f)), _ctx(agent_state))


async def test_read_empty_file(tmp_path):
    f = tmp_path / "empty.txt"; f.write_text("")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await read_tool().func(ReadIn(file_path=str(f)), _ctx(agent_state))
    assert isinstance(result, str)
    assert "empty" in result.lower()


async def test_read_offset_out_of_range(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("only one line\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await read_tool().func(
        ReadIn(file_path=str(f), offset=99), _ctx(agent_state))
    assert isinstance(result, str)
    assert "out of range" in result.lower() or "shorter" in result.lower()


async def test_read_nonexistent_raises(tmp_path):
    agent_state = AgentState(cwd=str(tmp_path))
    import pytest
    with pytest.raises(Exception):
        await read_tool().func(
            ReadIn(file_path=str(tmp_path / "nope.txt")), _ctx(agent_state))


async def test_read_out_of_range_then_external_change_blocks_write(tmp_path):
    """越界 read 也记 read_state → 外部改后 write 被拒(I-1 回归)。

    反例: 若越界分支不 set, write 会因无 read_state 记录而 is_stale=False,
    静默覆盖外部改动 → 陈旧保护失效。

    Task 3: read/write 共享同一 agent_state.file_read_state(func 从 ctx 取)。
    """
    import os
    import pytest
    from core.builtin_tools.write import WriteIn, write_tool

    f = tmp_path / "a.txt"; f.write_text("only one line\n")
    agent_state = AgentState(cwd=str(tmp_path))
    # 越界 read: 拿 warning, 但应记 read_state
    await read_tool().func(
        ReadIn(file_path=str(f), offset=99), _ctx(agent_state))
    # 外部改文件(mtime 推后)
    future = os.path.getmtime(str(f)) + 1000
    os.utime(str(f), (future, future))
    # write 应被拒(陈旧检测生效)
    with pytest.raises(PermissionError):
        await write_tool().func(
            WriteIn(file_path=str(f), content="x\n"), _ctx(agent_state))
