"""MCP progress notification 治理测试。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from core.mcp import MCPManager, MCPServerConfig, MCPToolSpec, create_mcp_tool
from core.tools import ToolContext
from telemetry.events import TraceKind
from telemetry.tracer import NoopTracer


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_demo_server.py"


class RecordingTracer(NoopTracer):
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_client_collects_progress_without_changing_final_content():
    manager = MCPManager(
        [MCPServerConfig(name="demo", command=sys.executable, args=[str(FIXTURE)])]
    )
    events = []
    try:
        await manager.start()
        result = await manager.call_tool(
            "demo",
            "progress_then_text",
            {},
            progress_callback=events.append,
        )
    finally:
        await manager.close()

    assert result.content == "done"
    assert [event.message for event in events] == ["halfway"]
    assert [event.message for event in result.progress] == ["halfway"]


@pytest.mark.asyncio
async def test_mcp_tool_emits_progress_trace_event():
    tracer = RecordingTracer()
    manager = MCPManager(
        [MCPServerConfig(name="demo", command=sys.executable, args=[str(FIXTURE)])]
    )
    spec = MCPToolSpec(
        server_name="demo",
        name="progress_then_text",
        description="Return progress before final text",
        input_schema={"type": "object", "properties": {}},
    )
    tool = create_mcp_tool(spec, manager)
    ctx = ToolContext(tracer=tracer, abort_signal=asyncio.Event())

    try:
        await manager.start()
        output = await tool.func(tool.input_model(), ctx)
    finally:
        await manager.close()

    assert output == "done"
    progress_events = [
        event for event in tracer.events
        if event.kind == TraceKind.TOOL_EXEC_PROGRESS
    ]
    assert len(progress_events) == 1
    assert progress_events[0].payload == {
        "server_name": "demo",
        "tool_name": "progress_then_text",
        "progress": 1,
        "total": 2,
        "message": "halfway",
    }
