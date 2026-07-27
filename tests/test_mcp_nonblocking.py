"""MCP 非阻塞连接专项测试。"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from core.agent_loop import AgentConfig
from core.mcp import MCPManager, MCPServerConfig, MCPServerHealth, MCPServerState

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_demo_server.py"


async def test_slow_fixture_can_delay_tools_list():
    manager = MCPManager(
        [
            MCPServerConfig(
                name="slow",
                command=sys.executable,
                args=[str(FIXTURE)],
                env={"MCP_DEMO_LIST_DELAY": "0.3"},
                timeout=1.0,
            )
        ]
    )
    try:
        await manager.start()
        started = time.monotonic()
        await manager.list_tools()
        elapsed = time.monotonic() - started
        assert elapsed >= 0.25
    finally:
        await manager.close()


def test_mcp_health_types_are_exported():
    health = MCPServerHealth(
        name="demo",
        state=MCPServerState.DISCONNECTED,
        error=None,
        tool_count=0,
    )

    assert health.name == "demo"
    assert health.state.value == "disconnected"


async def test_get_tools_does_not_wait_for_slow_mcp_server():
    manager = MCPManager(
        [
            MCPServerConfig(
                name="slow",
                command=sys.executable,
                args=[str(FIXTURE)],
                env={"MCP_DEMO_LIST_DELAY": "1.0"},
                timeout=2.0,
            )
        ],
        tool_wait_timeout=0.0,
    )
    started = time.monotonic()
    try:
        tools = await manager.get_tools()
        elapsed = time.monotonic() - started

        assert tools == []
        assert elapsed < 0.3
        assert manager.health()[0].state in {
            MCPServerState.CONNECTING,
            MCPServerState.READY,
        }
    finally:
        await manager.close()


async def test_get_tools_returns_cached_tools_after_background_finishes():
    manager = MCPManager(
        [
            MCPServerConfig(
                name="slow",
                command=sys.executable,
                args=[str(FIXTURE)],
                env={"MCP_DEMO_LIST_DELAY": "0.2"},
                timeout=1.0,
            )
        ],
        tool_wait_timeout=0.0,
    )
    try:
        assert await manager.get_tools() == []
        await asyncio.sleep(0.35)

        names = [tool.name for tool in await manager.get_tools()]

        assert names == [
            "mcp__slow__echo_text",
            "mcp__slow__large_text",
            "mcp__slow__structured_json",
            "mcp__slow__dominant_text",
            "mcp__slow__progress_then_text",
        ]
        assert manager.health()[0].state == MCPServerState.READY
    finally:
        await manager.close()


async def test_close_cancels_background_connect_task():
    manager = MCPManager(
        [
            MCPServerConfig(
                name="slow",
                command=sys.executable,
                args=[str(FIXTURE)],
                env={"MCP_DEMO_LIST_DELAY": "1.0"},
                timeout=2.0,
            )
        ]
    )
    await manager.start_background()
    await asyncio.sleep(0.05)

    await manager.close()

    assert manager.health()[0].state in {
        MCPServerState.DISCONNECTED,
        MCPServerState.FAILED,
    }


async def test_bad_mcp_command_does_not_escape_get_tools():
    manager = MCPManager(
        [
            MCPServerConfig(
                name="bad",
                command="/definitely/not/a/real/mcp/server",
                args=[],
                timeout=0.1,
            )
        ],
        tool_wait_timeout=0.1,
    )
    try:
        tools = await manager.get_tools()
        health = manager.health()[0]

        assert tools == []
        assert health.name == "bad"
        assert health.state == MCPServerState.FAILED
        assert health.error
    finally:
        await manager.close()


async def test_start_still_raises_for_bad_mcp_command():
    manager = MCPManager(
        [
            MCPServerConfig(
                name="bad",
                command="/definitely/not/a/real/mcp/server",
                args=[],
                timeout=0.1,
            )
        ]
    )
    try:
        try:
            await manager.start()
        except Exception:
            health = manager.health()[0]
            assert health.state == MCPServerState.FAILED
            assert health.error
        else:
            raise AssertionError("manager.start() should keep fail-fast behavior")
    finally:
        await manager.close()


async def test_agent_config_resolve_tools_keeps_builtins_when_mcp_is_slow():
    manager = MCPManager(
        [
            MCPServerConfig(
                name="slow",
                command=sys.executable,
                args=[str(FIXTURE)],
                env={"MCP_DEMO_LIST_DELAY": "1.0"},
                timeout=2.0,
            )
        ],
        tool_wait_timeout=0.0,
    )
    cfg = AgentConfig(
        provider=None,
        system="x",
        model="m",
        max_tokens=1,
        mcp_manager=manager,
    )
    started = time.monotonic()
    try:
        tools = await cfg.resolve_tools()
        elapsed = time.monotonic() - started
        names = [tool.name for tool in tools]

        assert elapsed < 0.3
        assert "Read" in names
        assert "Bash" in names
        assert "mcp__slow__echo_text" not in names
    finally:
        await manager.close()


async def test_start_then_get_tools_still_returns_tools_immediately():
    manager = MCPManager(
        [MCPServerConfig(name="demo", command=sys.executable, args=[str(FIXTURE)])],
        tool_wait_timeout=0.0,
    )
    try:
        await manager.start()
        started = time.monotonic()
        names = [tool.name for tool in await manager.get_tools()]
        elapsed = time.monotonic() - started

        assert names == [
            "mcp__demo__echo_text",
            "mcp__demo__large_text",
            "mcp__demo__structured_json",
            "mcp__demo__dominant_text",
            "mcp__demo__progress_then_text",
        ]
        assert elapsed < 0.3
        assert manager.health()[0].state == MCPServerState.READY
    finally:
        await manager.close()
