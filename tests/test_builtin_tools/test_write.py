# tests/test_builtin_tools/test_write.py
import asyncio
import os

from core.builtin_tools.read import ReadIn, read_tool
from core.builtin_tools.write import WriteIn, write_tool
from core.tools import ToolContext
from core.types import AgentState
from telemetry.tracer import NoopTracer


def _ctx(agent_state: AgentState) -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=agent_state)


async def test_write_creates_new_file(tmp_path):
    agent_state = AgentState(cwd=str(tmp_path))
    f = tmp_path / "new.txt"
    result = await write_tool().func(
        WriteIn(file_path=str(f), content="hello\n"), _ctx(agent_state))
    assert isinstance(result, str)
    assert f.read_text() == "hello\n"
    assert "created" in result.lower()


async def test_write_update_returns_diff(tmp_path):
    agent_state = AgentState(cwd=str(tmp_path))
    f = tmp_path / "a.txt"; f.write_text("line1\nline2\n")
    # 先 read 记录 mtime, 才能 update(否则首次写视为 create? 不: 文件存在且 read_state 无记录 → 允许写, 算 update)
    result = await write_tool().func(
        WriteIn(file_path=str(f), content="line1\nCHANGED\n"), _ctx(agent_state))
    assert isinstance(result, str)
    assert f.read_text() == "line1\nCHANGED\n"
    assert "updated" in result.lower()
    assert "CHANGED" in result or "line2" in result   # diff 含改动


async def test_write_stale_after_external_change(tmp_path):
    """read 后外部改 mtime → write 被拒(is_error)。Task 3: 共享 agent_state.file_read_state。"""
    agent_state = AgentState(cwd=str(tmp_path))
    f = tmp_path / "a.txt"; f.write_text("orig\n")
    rt = read_tool()
    await rt.func(ReadIn(file_path=str(f)), _ctx(agent_state))   # read 记录
    # 外部改文件(mtime 推后)
    f.write_text("EXTERNAL EDIT\n")
    future = os.path.getmtime(str(f)) + 1000
    os.utime(str(f), (future, future))
    import pytest
    with pytest.raises(Exception) as exc:
        await write_tool().func(
            WriteIn(file_path=str(f), content="mine\n"), _ctx(agent_state))
    assert "modified" in str(exc.value).lower() or "read" in str(exc.value).lower()


async def test_write_mkdir_parents(tmp_path):
    agent_state = AgentState(cwd=str(tmp_path))
    f = tmp_path / "sub" / "dir" / "a.txt"
    result = await write_tool().func(
        WriteIn(file_path=str(f), content="x\n"), _ctx(agent_state))
    assert isinstance(result, str)
    assert f.read_text() == "x\n"


async def test_write_after_read_same_mtime_succeeds(tmp_path):
    """read 后文件未变 → write 成功(read-write 正常流)。"""
    agent_state = AgentState(cwd=str(tmp_path))
    f = tmp_path / "a.txt"; f.write_text("orig\n")
    await read_tool().func(ReadIn(file_path=str(f)), _ctx(agent_state))
    result = await write_tool().func(
        WriteIn(file_path=str(f), content="new\n"), _ctx(agent_state))
    assert isinstance(result, str)
    assert f.read_text() == "new\n"
    assert "updated" in result.lower()
