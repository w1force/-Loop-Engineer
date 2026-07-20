"""tools: Tool.to_schema / default_can_use_tool / ToolContext / Tool 新字段。"""
import asyncio

import pytest
from pydantic import BaseModel

from core.tools import CanUseDecision, Tool, ToolContext, _not_impl, default_can_use_tool
from core.types import AgentState, ToolUseBlock
from telemetry.tracer import NoopTracer


class EchoInput(BaseModel):
    msg: str


async def _echo(inp: EchoInput, ctx: ToolContext) -> str:
    return inp.msg


def test_tool_to_schema_generates_json_schema():
    t = Tool(name="echo", description="echo back", input_model=EchoInput, func=_echo)
    schema = t.to_schema()
    assert schema["name"] == "echo"
    assert schema["input_schema"]["type"] == "object"
    assert "msg" in schema["input_schema"]["properties"]


async def test_default_can_use_tool_allows():
    decision = await default_can_use_tool(ToolUseBlock(id="c1", name="echo", input={}))
    assert isinstance(decision, CanUseDecision)
    assert decision.allow is True


def test_tool_defaults_is_concurrency_safe_false_and_no_pre_execute():
    t = Tool(name="echo", description="d", input_model=EchoInput, func=_echo)
    assert t.is_concurrency_safe is False
    assert t.pre_execute is None


def test_tool_context_carries_fields():
    # Task 2: agent_state 必需;query_state 默认 None
    ctx = ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=AgentState(),
    )
    assert ctx.query_state is None


def test_not_impl_raises_with_clear_message():
    with pytest.raises(NotImplementedError, match="tool execution"):
        _not_impl("tool execution", "Phase 2")
