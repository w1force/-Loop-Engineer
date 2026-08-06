"""MCP FAILED 后自动重连专项测试。"""
from __future__ import annotations

import asyncio

from core.mcp import MCPManager, MCPServerConfig, MCPServerHealth, MCPServerState
from core.mcp.types import MCPTransport
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

        async def call_tool(self, name, arguments, *, options=None):
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


async def test_remote_failed_background_connect_retries_without_get_tools_trigger():
    """远程 transport 失败后由连接层按 backoff 自动重试,不是等下一次 get_tools。"""
    attempts = 0

    class RemoteClient:
        def __init__(self, should_fail: bool):
            self.config = MCPServerConfig(
                name="remote", transport=MCPTransport.HTTP, url="https://mcp.example.invalid"
            )
            self.should_fail = should_fail
            self.closed = False

        async def start(self):
            if self.should_fail:
                raise RuntimeError("temporary remote outage")

        async def list_tools(self):
            return [
                MCPToolSpec(
                    server_name="remote",
                    name="diagnose",
                    description="Diagnose runtime evidence",
                    input_schema={"type": "object", "properties": {}},
                )
            ]

        async def call_tool(self, name, arguments, *, options=None):
            raise AssertionError("not used")

        async def close(self):
            self.closed = True

    def factory(config):
        nonlocal attempts
        attempts += 1
        return RemoteClient(should_fail=attempts == 1)

    manager = MCPManager(
        [
            MCPServerConfig(
                name="remote",
                transport=MCPTransport.HTTP,
                url="https://mcp.example.invalid",
            )
        ],
        client_factory=factory,
        tool_wait_timeout=0.0,
        retry_initial_delay=0.01,
        retry_max_delay=0.01,
    )
    try:
        await manager.start_background()
        await asyncio.sleep(0.08)

        health = manager.health()[0]
        tools = await manager.get_ready_tools()

        assert attempts == 2
        assert health.state == MCPServerState.READY
        assert health.failure_count == 0
        assert health.next_retry_at is None
        assert [tool.name for tool in tools] == ["mcp__remote__diagnose"]
    finally:
        await manager.close()
