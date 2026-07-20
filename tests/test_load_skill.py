"""Task 3: load_skill 工具测试。

Task 3 起 load_skill_tool 是 core/builtin_tools/load_skill.py 的模块级常量,
不再闭包捕获 metas;func 从 ctx.agent_state.skills 取(无参工厂模式)。
"""
import asyncio

from core.builtin_tools.load_skill import LoadSkillInput, load_skill_tool
from core.skills import SkillLoader
from core.tools import Tool, ToolContext
from core.types import AgentState
from telemetry.tracer import NoopTracer


def _ctx(agent_state: AgentState) -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=agent_state)


def _make_skill(root, name, body):
    d = root / name; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


async def test_load_skill_returns_full_text(tmp_path):
    _make_skill(tmp_path, "foo", "---\ndescription: foo\n---\n# body\ninstructions\n")
    agent_state = AgentState(skills=SkillLoader.scan([tmp_path]))
    result = await load_skill_tool.func(LoadSkillInput(name="foo"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "# body" in result
    assert "instructions" in result
    assert "description: foo" in result  # 全文含 frontmatter


async def test_load_skill_not_found(tmp_path):
    _make_skill(tmp_path, "foo", "---\ndescription: foo\n---\n# foo\n")
    agent_state = AgentState(skills=SkillLoader.scan([tmp_path]))
    result = await load_skill_tool.func(LoadSkillInput(name="bar"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "not found" in result.lower()
    assert "foo" in result  # Available 列表含可用 skill


async def test_load_skill_read_failure(tmp_path):
    _make_skill(tmp_path, "foo", "---\ndescription: foo\n---\n# foo\n")
    agent_state = AgentState(skills=SkillLoader.scan([tmp_path]))
    (tmp_path / "foo" / "SKILL.md").unlink()  # 模拟读失败
    result = await load_skill_tool.func(LoadSkillInput(name="foo"), _ctx(agent_state))
    assert isinstance(result, str)
    assert "error" in result.lower()


def test_load_skill_is_concurrency_safe():
    assert isinstance(load_skill_tool, Tool)
    assert load_skill_tool.is_concurrency_safe is True
    assert load_skill_tool.name == "load_skill"


def test_load_skill_no_closure():
    """Task 3: load_skill_tool 是模块级常量,func 不闭包 metas(从 ctx.agent_state.skills 取)。"""
    assert load_skill_tool.func.__closure__ is None
