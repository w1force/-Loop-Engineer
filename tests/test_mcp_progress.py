"""MCP progress notification 治理测试。"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

from core.mcp import (
    MCPManager,
    MCPServerConfig,
    MCPToolExecutionPolicy,
    MCPToolSpec,
    create_mcp_tool,
)
from core.tools import ToolContext
from telemetry.events import TraceKind
from telemetry.tracer import NoopTracer


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_demo_server.py"
LIFECYCLE_FIXTURE = Path(__file__).parent / "fixtures" / "mcp_lifecycle_server.py"


class RecordingTracer(NoopTracer):
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class FailingTracer(NoopTracer):
    def emit(self, event):
        raise RuntimeError("trace sink unavailable")


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


@pytest.mark.asyncio
async def test_progress_trace_failure_is_observable_without_losing_tool_result(caplog):
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
    ctx = ToolContext(tracer=FailingTracer(), abort_signal=asyncio.Event())
    caplog.set_level(logging.DEBUG, logger="core.mcp.tool_adapter")

    try:
        await manager.start()
        output = await tool.func(tool.input_model(), ctx)
    finally:
        await manager.close()

    assert output == "done"
    assert "MCP progress trace failed for demo.progress_then_text" in caplog.text
    assert "trace sink unavailable" in caplog.text


@pytest.mark.asyncio
async def test_heartbeat_trace_keeps_client_observation_context():
    tracer = RecordingTracer()
    manager = MCPManager(
        [
            MCPServerConfig(
                name="life",
                command=sys.executable,
                args=[str(LIFECYCLE_FIXTURE)],
            )
        ],
        execution_policy=MCPToolExecutionPolicy(
            timeout_seconds=0.3,
            heartbeat_seconds=0.02,
        ),
    )
    spec = MCPToolSpec(
        server_name="life",
        name="sleep_echo",
        description="Return a value after a delay",
        input_schema={
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "delay": {"type": "number"},
                "progress": {"type": "boolean"},
            },
            "required": ["value"],
        },
    )
    tool = create_mcp_tool(spec, manager)
    ctx = ToolContext(tracer=tracer, abort_signal=asyncio.Event())

    try:
        await manager.start()
        output = await tool.func(
            tool.input_model(value="done", delay=0.06, progress=True),
            ctx,
        )
    finally:
        await manager.close()

    assert output == "done"
    payloads = [
        event.payload
        for event in tracer.events
        if event.kind == TraceKind.TOOL_EXEC_PROGRESS
    ]
    server_payload = next(payload for payload in payloads if payload["message"] == "working:done")
    assert server_payload == {
        "server_name": "life",
        "tool_name": "sleep_echo",
        "progress": 1,
        "total": 2,
        "message": "working:done",
    }
    heartbeat_payloads = [
        payload for payload in payloads if payload.get("source") == "heartbeat"
    ]
    assert heartbeat_payloads
    assert all(payload["elapsed_seconds"] > 0 for payload in heartbeat_payloads)
    assert all(
        payload["received_server_progress"] is True
        for payload in heartbeat_payloads
    )
