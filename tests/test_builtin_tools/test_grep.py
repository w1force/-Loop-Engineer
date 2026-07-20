# tests/test_builtin_tools/test_grep.py
import asyncio
import os

from core.builtin_tools.grep import GrepIn, grep_tool
from core.tools import ToolContext
from core.types import AgentState
from telemetry.tracer import NoopTracer


def _ctx(agent_state: AgentState) -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=agent_state)


async def test_grep_files_with_matches(tmp_path):
    (tmp_path / "a.py").write_text("hello world\nfoo\n")
    (tmp_path / "b.py").write_text("bar\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await grep_tool().func(
        GrepIn(pattern="hello", output_mode="files_with_matches"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "a.py" in result
    assert "b.py" not in result
    assert "Found" in result


async def test_grep_content_mode_with_line_numbers(tmp_path):
    (tmp_path / "a.py").write_text("foo\nhello\nbar\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await grep_tool().func(
        GrepIn(pattern="hello", output_mode="content"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "hello" in result
    assert "a.py" in result


async def test_grep_count_mode(tmp_path):
    (tmp_path / "a.py").write_text("hello\nhello\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await grep_tool().func(
        GrepIn(pattern="hello", output_mode="count"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "a.py" in result
    assert "occurrences" in result


async def test_grep_no_matches(tmp_path):
    (tmp_path / "a.py").write_text("foo\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await grep_tool().func(
        GrepIn(pattern="zzz"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "No files found" in result or "No matches" in result


async def test_grep_type_filter(tmp_path):
    (tmp_path / "a.py").write_text("hello\n")
    (tmp_path / "b.txt").write_text("hello\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await grep_tool().func(
        GrepIn(pattern="hello", type="py"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "a.py" in result
    assert "b.txt" not in result


async def test_grep_mtime_sort_order(tmp_path):
    import time
    old = tmp_path / "old.py"; old.write_text("x\n")
    new = tmp_path / "new.py"; new.write_text("x\n")
    os.utime(old, (time.time() - 1000, time.time() - 1000))  # old 更旧
    os.utime(new, (time.time(), time.time()))                 # new 更新
    agent_state = AgentState(cwd=str(tmp_path))
    result = await grep_tool().func(
        GrepIn(pattern="x"), _ctx(agent_state))
    assert isinstance(result, str)
    # new(mtime 大)排在 old 前
    assert result.index("new.py") < result.index("old.py")


async def test_grep_pattern_starting_with_dash(tmp_path):
    (tmp_path / "a.py").write_text("foo-bar\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await grep_tool().func(
        GrepIn(pattern="-bar", output_mode="content"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "foo-bar" in result


async def test_grep_rg_missing(monkeypatch):
    """rg 未装 → is_error 带安装提示(模拟 FileNotFoundError)。"""
    import core.builtin_tools.grep as g

    async def _boom(*a, **kw):
        raise FileNotFoundError("rg")
    monkeypatch.setattr(g.asyncio, "create_subprocess_exec", _boom)
    agent_state = AgentState(cwd="/tmp")
    with __import__("pytest").raises(RuntimeError) as exc:
        await grep_tool().func(GrepIn(pattern="x"), _ctx(agent_state))
    assert "ripgrep" in str(exc.value).lower() or "rg" in str(exc.value).lower()
