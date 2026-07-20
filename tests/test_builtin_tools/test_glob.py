# tests/test_builtin_tools/test_glob.py
import asyncio

from core.builtin_tools.glob import GlobIn, glob_tool
from core.tools import ToolContext
from core.types import AgentState
from telemetry.tracer import NoopTracer


def _ctx(agent_state: AgentState) -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=agent_state)


async def test_glob_matches_pattern(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await glob_tool().func(GlobIn(pattern="*.py"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "a.py" in result
    assert "b.txt" not in result


async def test_glob_relative_paths(tmp_path):
    (tmp_path / "a.py").write_text("x")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await glob_tool().func(GlobIn(pattern="*.py"), _ctx(agent_state))
    assert isinstance(result, str)
    assert result == "a.py"   # 相对 cwd, 不含 tmp_path 前缀


async def test_glob_excludes_git(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "a.py").write_text("x")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await glob_tool().func(GlobIn(pattern="**/*"), _ctx(agent_state))
    assert isinstance(result, str)
    assert ".git" not in result
    assert "a.py" in result


async def test_glob_no_files(tmp_path):
    agent_state = AgentState(cwd=str(tmp_path))
    result = await glob_tool().func(GlobIn(pattern="*.nope"), _ctx(agent_state))
    assert isinstance(result, str)
    assert result == "No files found"


async def test_glob_truncates_at_100(tmp_path):
    for i in range(150):
        (tmp_path / f"f{i:03d}.py").write_text("x")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await glob_tool().func(GlobIn(pattern="*.py"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "truncated" in result.lower()
