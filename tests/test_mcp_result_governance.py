"""MCP 大结果治理端到端测试。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from core.agent_loop import AgentConfig
from core.mcp import MCPManager, MCPServerConfig
from core.mcp.result_policy import MCPResultPolicy
from core.tool_executor import BatchToolExecutor
from core.tools import ToolContext, default_can_use_tool
from core.types import AgentState, QueryState, ToolUseBlock
from telemetry.tracer import NoopTracer

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_demo_server.py"


async def test_mcp_large_result_is_truncated_before_executor_result(tmp_path):
    manager = MCPManager(
        [MCPServerConfig(name="demo", command=sys.executable, args=[str(FIXTURE)])],
        result_policy=MCPResultPolicy(max_inline_chars=120, artifact_dir=tmp_path),
    )
    try:
        await manager.start()
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
        mcp_tool = tool_by_name["mcp__demo__large_text"]
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
                id="large_call_1",
                name="mcp__demo__large_text",
                input={"size": 500},
            )
        )

        results = await executor.get_results()

        assert len(results) == 1
        assert results[0].is_error is False
        assert "MCP output truncated" in results[0].content
        assert "full output:" in results[0].content
        assert "L" * 500 not in results[0].content
        artifact_hint = results[0].content.rsplit("full output: ", 1)[1].rstrip("]")
        artifact = Path(artifact_hint)
        assert artifact.exists()
        assert artifact.read_text(encoding="utf-8") == "L" * 500
    finally:
        await manager.close()
