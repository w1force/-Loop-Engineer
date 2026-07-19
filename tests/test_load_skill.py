"""Task 3: load_skill 工具测试。"""
import asyncio

from core.skills import SkillLoader, load_skill_tool, LoadSkillInput
from core.tools import Tool, ToolContext
from telemetry.tracer import NoopTracer


def _ctx() -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())


def _make_skill(root, name, body):
    d = root / name; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


async def test_load_skill_returns_full_text(tmp_path):
    _make_skill(tmp_path, "foo", "---\ndescription: foo\n---\n# body\ninstructions\n")
    tool = load_skill_tool(SkillLoader.scan([tmp_path]))
    result = await tool.func(LoadSkillInput(name="foo"), _ctx())
    assert isinstance(result, str)
    assert "# body" in result
    assert "instructions" in result
    assert "description: foo" in result  # 全文含 frontmatter


async def test_load_skill_not_found(tmp_path):
    _make_skill(tmp_path, "foo", "---\ndescription: foo\n---\n# foo\n")
    tool = load_skill_tool(SkillLoader.scan([tmp_path]))
    result = await tool.func(LoadSkillInput(name="bar"), _ctx())
    assert isinstance(result, str)
    assert "not found" in result.lower()
    assert "foo" in result  # Available 列表含可用 skill


async def test_load_skill_read_failure(tmp_path):
    _make_skill(tmp_path, "foo", "---\ndescription: foo\n---\n# foo\n")
    tool = load_skill_tool(SkillLoader.scan([tmp_path]))
    (tmp_path / "foo" / "SKILL.md").unlink()  # 模拟读失败
    result = await tool.func(LoadSkillInput(name="foo"), _ctx())
    assert isinstance(result, str)
    assert "error" in result.lower()


def test_load_skill_is_concurrency_safe(tmp_path):
    _make_skill(tmp_path, "foo", "---\ndescription: foo\n---\n# foo\n")
    tool = load_skill_tool(SkillLoader.scan([tmp_path]))
    assert isinstance(tool, Tool)
    assert tool.is_concurrency_safe is True
    assert tool.name == "load_skill"
