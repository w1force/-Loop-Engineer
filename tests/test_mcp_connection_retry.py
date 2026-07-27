"""MCP FAILED 后自动重连专项测试。"""
from __future__ import annotations

from core.mcp import MCPManager, MCPServerConfig, MCPServerHealth, MCPServerState
from core.mcp.types import MCPToolSpec


def test_mcp_health_exposes_retry_fields():
    health = MCPServerHealth(
        name="demo",
        state=MCPServerState.FAILED,
        error="boom",
        tool_count=0,
        failure_count=2,
        next_retry_at=14.0,
        last_attempt_at=10.0,
        last_success_at=8.0,
    )

    assert health.failure_count == 2
    assert health.next_retry_at == 14.0
    assert health.last_attempt_at == 10.0
    assert health.last_success_at == 8.0


async def test_failed_connect_schedules_retry():
    now = 100.0
    manager = MCPManager(
        [
            MCPServerConfig(
                name="bad",
                command="/definitely/not/a/real/mcp/server",
                timeout=0.1,
            )
        ],
        tool_wait_timeout=0.1,
        retry_initial_delay=5.0,
        clock=lambda: now,
    )
    try:
        tools = await manager.get_tools()
        health = manager.health()[0]

        assert tools == []
        assert health.state == MCPServerState.FAILED
        assert health.failure_count == 1
        assert health.last_attempt_at == 100.0
        assert health.next_retry_at == 105.0
    finally:
        await manager.close()


async def test_failed_server_does_not_retry_before_next_retry_at():
    now = 100.0
    manager = MCPManager(
        [
            MCPServerConfig(
                name="bad",
                command="/definitely/not/a/real/mcp/server",
                timeout=0.1,
            )
        ],
        tool_wait_timeout=0.1,
        retry_initial_delay=10.0,
        clock=lambda: now,
    )
    try:
        await manager.get_tools()
        first = manager.health()[0]

        await manager.get_tools()
        second = manager.health()[0]

        assert first.failure_count == 1
        assert second.failure_count == 1
        assert second.next_retry_at == first.next_retry_at
    finally:
        await manager.close()


async def test_failed_server_retries_after_next_retry_at():
    current = {"now": 100.0}
    manager = MCPManager(
        [
            MCPServerConfig(
                name="bad",
                command="/definitely/not/a/real/mcp/server",
                timeout=0.1,
            )
        ],
        tool_wait_timeout=0.1,
        retry_initial_delay=10.0,
        retry_max_delay=30.0,
        clock=lambda: current["now"],
    )
    try:
        await manager.get_tools()
        assert manager.health()[0].failure_count == 1

        current["now"] = 111.0
        await manager.get_tools()
        health = manager.health()[0]

        assert health.state == MCPServerState.FAILED
        assert health.failure_count == 2
        assert health.last_attempt_at == 111.0
        assert health.next_retry_at == 131.0
    finally:
        await manager.close()


async def test_retry_success_resets_failure_state():
    class FailingClient:
        async def start(self):
            raise RuntimeError("temporary down")

        async def list_tools(self):
            return []

        async def close(self):
            pass

    class WorkingClient:
        async def start(self):
            pass

        async def list_tools(self):
            return [
                MCPToolSpec(
                    server_name="demo",
                    name="echo_text",
                    description="Return text",
                    input_schema={"type": "object", "properties": {}},
                )
            ]

        async def call_tool(self, name, arguments, *, progress_callback=None):
            raise AssertionError("not used")

        async def close(self):
            pass

    manager = MCPManager(
        [MCPServerConfig(name="demo", command="unused")],
        tool_wait_timeout=0.1,
        retry_initial_delay=0.0,
        clock=lambda: 100.0,
    )
    manager._clients["demo"] = FailingClient()  # retry 测试里替换传输层,避免真实进程。
    try:
        assert await manager.get_tools() == []
        assert manager.health()[0].failure_count == 1

        manager._clients["demo"] = WorkingClient()
        tools = await manager.get_tools()
        health = manager.health()[0]

        assert [tool.name for tool in tools] == ["mcp__demo__echo_text"]
        assert health.state == MCPServerState.READY
        assert health.failure_count == 0
        assert health.next_retry_at is None
        assert health.last_success_at == 100.0
    finally:
        await manager.close()
