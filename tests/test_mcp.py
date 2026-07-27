"""MCP 基础框架测试。"""
from __future__ import annotations

import sys
import asyncio
from pathlib import Path

from core.agent_loop import AgentConfig
from core.mcp import MCPManager, MCPServerConfig, MCPToolSpec, create_mcp_tool
from core.mcp.result_policy import MCPResultPolicy
from core.mcp.types import MCPToolResult
from core.tools import Tool
from core.tools import ToolContext
from core.types import AgentState, QueryState
from telemetry.tracer import NoopTracer


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_demo_server.py"


def _ctx() -> ToolContext:
    return ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=AgentState(),
        query_state=QueryState.model_construct(messages=[]),
    )


async def test_stdio_client_lists_and_calls_tools():
    manager = MCPManager(
        [MCPServerConfig(name="demo", command=sys.executable, args=[str(FIXTURE)])]
    )
    try:
        await manager.start()
        specs = await manager.list_tools()
        assert [s.name for s in specs] == [
            "echo_text",
            "large_text",
            "structured_json",
            "dominant_text",
            "progress_then_text",
        ]
        assert specs[0].server_name == "demo"

        result = await manager.call_tool("demo", "echo_text", {"text": "hello"})
        assert result.is_error is False
        assert result.content == "hello"
    finally:
        await manager.close()


async def test_create_mcp_tool_wraps_mcp_call_as_project_tool():
    class FakeManager:
        async def call_tool(self, server_name: str, tool_name: str, arguments: dict):
            assert server_name == "demo"
            assert tool_name == "echo_text"
            assert arguments == {"text": "hello"}
            return "hello"

    spec = MCPToolSpec(
        server_name="demo",
        name="echo_text",
        description="Return text",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    tool = create_mcp_tool(spec, FakeManager())

    assert isinstance(tool, Tool)
    assert tool.name == "mcp__demo__echo_text"
    assert tool.description.startswith("[MCP:demo]")
    validated = tool.input_model.model_validate({"text": "hello"})
    assert await tool.func(validated, _ctx()) == "hello"


async def test_create_mcp_tool_applies_result_policy_to_large_output(tmp_path):
    class FakeManager:
        async def call_tool(self, server_name: str, tool_name: str, arguments: dict):
            return MCPToolResult(content="x" * 80)

    spec = MCPToolSpec(
        server_name="demo",
        name="large_text",
        description="Return large text",
        input_schema={"type": "object"},
    )
    tool = create_mcp_tool(
        spec,
        FakeManager(),
        result_policy=MCPResultPolicy(max_inline_chars=20, artifact_dir=tmp_path),
    )

    output = await tool.func(tool.input_model.model_validate({}), _ctx())

    assert output.startswith("x" * 20)
    assert "MCP output truncated" in output
    assert "full output:" in output
    assert "x" * 80 not in output


async def test_create_mcp_tool_keeps_error_result_behavior():
    class FakeManager:
        async def call_tool(self, server_name: str, tool_name: str, arguments: dict):
            return MCPToolResult(content="boom", is_error=True)

    spec = MCPToolSpec(
        server_name="demo",
        name="fail",
        description="Fail",
        input_schema={"type": "object"},
    )
    tool = create_mcp_tool(spec, FakeManager())

    try:
        await tool.func(tool.input_model.model_validate({}), _ctx())
    except ValueError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("MCP error result should still raise")


async def test_agent_config_combines_builtin_tools_with_mcp_tools():
    class FakeManager:
        async def get_tools(self):
            return [
                create_mcp_tool(
                    MCPToolSpec(
                        server_name="demo",
                        name="echo_text",
                        description="Return text",
                        input_schema={
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    ),
                    self,
                )
            ]

        async def call_tool(self, server_name: str, tool_name: str, arguments: dict):
            return "unused"

    cfg = AgentConfig(
        provider=None,
        system="x",
        model="m",
        max_tokens=1,
        tools=[],
        mcp_manager=FakeManager(),
    )

    tools = await cfg.resolve_tools()
    names = [t.name for t in tools]
    assert "Read" in names
    assert "Bash" in names
    assert "mcp__demo__echo_text" in names
