"""JProfiler-shaped MCP tools can flow through the base query loop.

This test uses a fake MCP client only to prove wiring:
MCPManager -> create_mcp_tool -> AgentConfig.resolve_tools -> query_loop
tool_use -> tool_result -> next turn. It does not claim real JProfiler analysis.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import respx

from core.agent_loop import AgentConfig
from core.loop.orchestrator import QueryParams, query_loop
from core.mcp import MCPManager, MCPServerConfig, MCPToolResult, MCPToolSpec
from core.providers.anthropic import AnthropicAdapter
from core.types import AgentState, AssistantMessage, TextBlock, UserMessage
from telemetry.tracer import NoopTracer

BASE = "https://api.anthropic.com"


class _JProfilerLikeClient:
    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.called: list[tuple[str, dict]] = []

    async def start(self) -> None:
        return None

    async def list_tools(self) -> list[MCPToolSpec]:
        return [
            MCPToolSpec(
                server_name=self.config.name,
                name="runtime_summary",
                description="Return a Java runtime summary",
                input_schema={
                    "type": "object",
                    "properties": {"evidence_id": {"type": "string"}},
                    "required": ["evidence_id"],
                },
            )
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        options=None,
    ) -> MCPToolResult:
        self.called.append((name, arguments))
        return MCPToolResult(
            content=f"runtime summary for {arguments['evidence_id']}",
            structured_content={"tool": name, "arguments": arguments},
        )

    async def close(self) -> None:
        return None


def _sse(events: list[dict]) -> str:
    parts: list[str] = []
    for event in events:
        parts.append(f"event: {event['type']}")
        parts.append(f"data: {json.dumps(event, ensure_ascii=False)}")
        parts.append("")
    return "\n".join(parts) + "\n"


ROUND1 = _sse(
    [
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 5, "output_tokens": 0}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "j1",
                "name": "mcp__JProfiler__runtime_summary",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"evidence_id":"runtime-evidence-demo"}',
            },
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]
)

ROUND2 = _sse(
    [
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 12, "output_tokens": 0}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "已读取 runtime summary"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 4},
        },
        {"type": "message_stop"},
    ]
)


@respx.mock
async def test_jprofiler_mcp_tool_can_roundtrip_through_query_loop():
    client = _JProfilerLikeClient(MCPServerConfig(name="JProfiler", command="unused"))
    manager = MCPManager(
        [client.config],
        client_factory=lambda _config: client,
        tool_wait_timeout=0.1,
    )
    responses = iter([httpx.Response(200, text=ROUND1), httpx.Response(200, text=ROUND2)])
    respx.post(f"{BASE}/v1/messages").mock(side_effect=lambda _req: next(responses))

    try:
        config = AgentConfig(
            provider=None,
            system="x",
            model="m",
            max_tokens=64,
            mcp_manager=manager,
        )
        tools = await config.resolve_tools()
        assert "mcp__JProfiler__runtime_summary" in {tool.name for tool in tools}

        adapter = AnthropicAdapter(api_key="k", base_url=BASE)
        params = QueryParams(
            system="",
            model="claude-sonnet-4-6",
            max_tokens=64,
            provider=adapter,
            abort_signal=asyncio.Event(),
            tools=tools,
            tool_execution_mode="streaming",
        )
        agent_state = AgentState(messages=[UserMessage(content="分析 runtime 证据")])
        out = [m async for m in query_loop(agent_state, params, NoopTracer())]

        assert client.called == [
            ("runtime_summary", {"evidence_id": "runtime-evidence-demo"})
        ]
        texts = [
            block.text
            for message in out
            if isinstance(message, AssistantMessage)
            for block in message.content
            if isinstance(block, TextBlock)
        ]
        assert "已读取 runtime summary" in texts
    finally:
        await manager.close()
