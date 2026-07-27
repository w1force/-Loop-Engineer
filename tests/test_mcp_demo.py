"""Demo-style test for the MCP tool path.

This is intentionally a little more narrative than the unit tests in
test_mcp.py: it shows the path a real agent turn uses after an MCP server is
configured.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from core.agent_loop import AgentConfig
from core.mcp import MCPManager, MCPServerConfig
from core.tool_executor import BatchToolExecutor
from core.tools import ToolContext, default_can_use_tool
from core.types import AgentState, QueryState, ToolUseBlock
from telemetry.tracer import NoopTracer


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_demo_server.py"


async def test_mcp_demo_exposes_schema_and_executes_like_agent_tool():
    """End-to-end demo: stdio MCP server -> Tool schema -> executor result."""
    manager = MCPManager(
        [MCPServerConfig(name="demo", command=sys.executable, args=[str(FIXTURE)])]
    )

    try:
        await manager.start()

        # AgentConfig is where MCP tools join the normal builtin/custom tool pool.
        cfg = AgentConfig(
            provider=None,
            system="",
            model="demo-model",
            max_tokens=64,
            tools=[],
            mcp_manager=manager,
        )
        tools = await cfg.resolve_tools()
        tool_by_name = {tool.name: tool for tool in tools}

        mcp_tool = tool_by_name["mcp__demo__echo_text"]
        assert mcp_tool.is_mcp is True
        assert mcp_tool.mcp_info == {
            "serverName": "demo",
            "toolName": "echo_text",
        }

        # The model sees this schema exactly like it sees builtin tool schemas.
        schema = mcp_tool.to_schema()
        assert schema["name"] == "mcp__demo__echo_text"
        assert schema["input_schema"]["required"] == ["text"]
        assert schema["input_schema"]["properties"]["text"]["type"] == "string"

        # Simulate the model choosing the MCP tool, then let the normal executor
        # call it. No special MCP-only executor path is needed.
        ctx = ToolContext(
            tracer=NoopTracer(),
            abort_signal=asyncio.Event(),
            agent_state=AgentState(),
            query_state=QueryState.model_construct(messages=[]),
        )
        executor = BatchToolExecutor(
            default_can_use_tool,
            NoopTracer(),
            ctx,
            tools=[mcp_tool],
        )
        executor.add_tool(
            ToolUseBlock(
                id="demo_call_1",
                name="mcp__demo__echo_text",
                input={"text": "hello from mcp"},
            )
        )

        results = await executor.get_results()
        assert len(results) == 1
        assert results[0].tool_use_id == "demo_call_1"
        assert results[0].is_error is False
        assert results[0].content == "hello from mcp"
    finally:
        await manager.close()
