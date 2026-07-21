# tests/test_builtin_tools/test_grep.py
import asyncio
import os

from core.builtin_tools.grep import GrepInput, GREP_TOOL
from core.tools import ToolContext
from core.types import AgentState
from telemetry.tracer import NoopTracer


def _ctx(agent_state: AgentState) -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=agent_state)


async def test_grep_files_with_matches(tmp_path):
    (tmp_path / "a.py").write_text("hello world\nfoo\n")
    (tmp_path / "b.py").write_text("bar\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await GREP_TOOL.func(
        GrepInput(pattern="hello", output_mode="files_with_matches"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "a.py" in result
    assert "b.py" not in result
    assert "找到" in result  # files_with_matches 中文输出 "找到 N 个文件"


async def test_grep_content_mode_with_line_numbers(tmp_path):
    (tmp_path / "a.py").write_text("foo\nhello\nbar\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await GREP_TOOL.func(
        GrepInput(pattern="hello", output_mode="content"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "hello" in result
    assert "a.py" in result


async def test_grep_count_mode(tmp_path):
    (tmp_path / "a.py").write_text("hello\nhello\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await GREP_TOOL.func(
        GrepInput(pattern="hello", output_mode="count"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "a.py" in result
    assert ":2" in result  # count 模式输出 "path:count";a.py 含 2 个 hello


async def test_grep_no_matches(tmp_path):
    (tmp_path / "a.py").write_text("foo\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await GREP_TOOL.func(
        GrepInput(pattern="zzz"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "No files found" in result or "No matches" in result


async def test_grep_mtime_sort_order(tmp_path):
    import time
    old = tmp_path / "old.py"; old.write_text("x\n")
    new = tmp_path / "new.py"; new.write_text("x\n")
    os.utime(old, (time.time() - 1000, time.time() - 1000))  # old 更旧
    os.utime(new, (time.time(), time.time()))                 # new 更新
    agent_state = AgentState(cwd=str(tmp_path))
    result = await GREP_TOOL.func(
        GrepInput(pattern="x"), _ctx(agent_state))
    assert isinstance(result, str)
    # new(mtime 大)排在 old 前
    assert result.index("new.py") < result.index("old.py")


async def test_grep_pattern_starting_with_dash(tmp_path):
    (tmp_path / "a.py").write_text("foo-bar\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await GREP_TOOL.func(
        GrepInput(pattern="-bar", output_mode="content"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "foo-bar" in result
