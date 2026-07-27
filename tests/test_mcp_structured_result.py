"""MCP 结构化结果保留测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.mcp import MCPManager, MCPServerConfig


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_demo_server.py"


@pytest.mark.asyncio
async def test_text_result_content_stays_exactly_the_same():
    manager = MCPManager(
        [MCPServerConfig(name="demo", command=sys.executable, args=[str(FIXTURE)])]
    )
    try:
        await manager.start()
        result = await manager.call_tool("demo", "echo_text", {"text": "hello"})
    finally:
        await manager.close()

    assert result.content == "hello"
    assert result.is_error is False
    assert result.raw_content == [{"type": "text", "text": "hello"}]
    assert result.structured_content is None


@pytest.mark.asyncio
async def test_structured_content_is_preserved_when_server_returns_it():
    manager = MCPManager(
        [MCPServerConfig(name="demo", command=sys.executable, args=[str(FIXTURE)])]
    )
    try:
        await manager.start()
        result = await manager.call_tool("demo", "structured_json", {})
    finally:
        await manager.close()

    assert result.structured_content == {
        "status": "failed",
        "root_cause": "timeout",
        "evidence": ["trace-1", "log-2"],
    }
    assert result.raw_content == []
    assert '"root_cause": "timeout"' in result.content
