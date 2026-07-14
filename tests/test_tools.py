"""tools: Tool.to_schema / default_can_use_tool / run_tools 桩。"""
import pytest
from pydantic import BaseModel

from core.tools import CanUseDecision, Tool, _not_impl, default_can_use_tool, run_tools
from core.types import ToolUseBlock
from telemetry.tracer import NoopTracer


class EchoInput(BaseModel):
    msg: str


async def _echo(inp: EchoInput) -> str:
    return inp.msg


def test_tool_to_schema_generates_json_schema():
    t = Tool(name="echo", description="echo back", input_model=EchoInput, func=_echo)
    schema = t.to_schema()
    assert schema["name"] == "echo"
    assert schema["description"] == "echo back"
    assert schema["input_schema"]["type"] == "object"
    assert "msg" in schema["input_schema"]["properties"]


async def test_default_can_use_tool_allows():
    decision = await default_can_use_tool(ToolUseBlock(id="c1", name="echo", input={}))
    assert isinstance(decision, CanUseDecision)
    assert decision.allow is True


async def test_run_tools_is_phase2_stub():
    with pytest.raises(NotImplementedError):
        await run_tools([], [], default_can_use_tool, NoopTracer())


def test_not_impl_raises_with_clear_message():
    with pytest.raises(NotImplementedError, match="tool execution"):
        _not_impl("tool execution", "Phase 2")
