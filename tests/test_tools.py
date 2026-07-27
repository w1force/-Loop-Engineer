"""tools: Tool.to_schema / default_can_use_tool / ToolContext / Tool 新字段。"""
import asyncio

import pytest
from pydantic import BaseModel

from core.file_state import FileStateCache
from core.tools import CanUseDecision, Tool, ToolContext, _not_impl, default_can_use_tool
from core.types import AgentState, QueryState, ToolUseBlock
from telemetry.tracer import NoopTracer


class EchoInput(BaseModel):
    msg: str


async def _echo(inp: EchoInput, ctx: ToolContext) -> str:
    return inp.msg


def _ctx() -> ToolContext:
    return ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=AgentState(),
        query_state=QueryState.model_construct(messages=[]),
    )


def test_tool_to_schema_generates_json_schema():
    t = Tool(name="echo", description="echo back", input_model=EchoInput, func=_echo)
    schema = t.to_schema()
    assert schema["name"] == "echo"
    assert schema["description"] == "echo back"
    assert schema["input_schema"]["type"] == "object"
    assert "msg" in schema["input_schema"]["properties"]


def test_tool_to_schema_prefers_input_json_schema_for_mcp_tools():
    input_json_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    t = Tool(
        name="mcp__logs__query",
        description="query logs",
        input_model=EchoInput,
        input_json_schema=input_json_schema,
        func=_echo,
        is_mcp=True,
        mcp_info={"serverName": "logs", "toolName": "query"},
    )

    schema = t.to_schema()

    assert schema["input_schema"] == input_json_schema
    assert t.is_mcp is True
    assert t.mcp_info == {"serverName": "logs", "toolName": "query"}


async def test_default_can_use_tool_allows():
    decision = await default_can_use_tool(ToolUseBlock(id="c1", name="echo", input={}))
    assert isinstance(decision, CanUseDecision)
    assert decision.allow is True


def test_tool_defaults_is_concurrency_safe_false_and_no_pre_execute():
    t = Tool(name="echo", description="d", input_model=EchoInput, func=_echo)
    assert t.is_concurrency_safe is False
    assert t.pre_execute is None
    assert t.is_mcp is False
    assert t.mcp_info is None


def test_tool_context_carries_fields():
    ctx = ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=AgentState(),
    )
    assert isinstance(ctx.agent_state.file_read_state, FileStateCache)


def test_not_impl_raises_with_clear_message():
    with pytest.raises(NotImplementedError, match="tool execution"):
        _not_impl("tool execution", "Phase 2")
