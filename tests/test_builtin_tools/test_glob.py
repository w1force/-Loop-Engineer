# tests/test_builtin_tools/test_glob.py
import asyncio

from core.builtin_tools.glob import GlobInput, GLOB_TOOL
from core.tools import ToolContext
from core.types import AgentState
from telemetry.tracer import NoopTracer


def _ctx(agent_state: AgentState) -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=agent_state)


async def test_glob_matches_pattern(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await GLOB_TOOL.func(GlobInput(pattern="*.py"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "a.py" in result
    assert "b.txt" not in result


async def test_glob_relative_paths(tmp_path):
    (tmp_path / "a.py").write_text("x")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await GLOB_TOOL.func(GlobInput(pattern="*.py"), _ctx(agent_state))
    assert isinstance(result, str)
    assert result.rstrip().endswith("a.py")  # 实现不相对化(对齐 CC 砍掉路径相对化),cwd 为绝对时返回绝对路径;校验末尾文件名


async def test_glob_excludes_git(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "a.py").write_text("x")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await GLOB_TOOL.func(GlobInput(pattern="**/*"), _ctx(agent_state))
    assert isinstance(result, str)
    assert ".git" not in result
    assert "a.py" in result


async def test_glob_no_files(tmp_path):
    agent_state = AgentState(cwd=str(tmp_path))
    result = await GLOB_TOOL.func(GlobInput(pattern="*.nope"), _ctx(agent_state))
    assert isinstance(result, str)
    assert result == "No files found"


async def test_glob_truncates_at_100(tmp_path):
    for i in range(150):
        (tmp_path / f"f{i:03d}.py").write_text("x")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await GLOB_TOOL.func(GlobInput(pattern="*.py"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "截断" in result  # 实现用中文截断提示"(结果已截断…)"
