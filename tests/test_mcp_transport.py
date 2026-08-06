"""MCP transport 可插拔框架测试。"""
from __future__ import annotations

import sys
from pathlib import Path

from core.mcp import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolResult,
    MCPToolSpec,
    MCPTransport,
)
from core.mcp.errors import MCPTransportUnsupportedError
from core.mcp.factory import create_mcp_client


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_demo_server.py"


class FakeMCPClient:
    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.closed = False

    async def start(self) -> None:
        pass

    async def list_tools(self) -> list[MCPToolSpec]:
        return [
            MCPToolSpec(
                server_name=self.config.name,
                name="fake_tool",
                description="Fake tool",
                input_schema={"type": "object"},
            )
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        options=None,
    ) -> MCPToolResult:
        return MCPToolResult(content=f"{self.config.name}:{name}:{arguments['value']}")

    async def close(self) -> None:
        self.closed = True


async def test_manager_uses_injected_client_factory_without_stdio_dependency():
    created: list[str] = []

    def factory(config: MCPServerConfig) -> FakeMCPClient:
        created.append(config.name)
        return FakeMCPClient(config)

    manager = MCPManager(
        [MCPServerConfig(name="fake", command="unused")],
        client_factory=factory,
        tool_wait_timeout=0.1,
    )

    try:
        tools = await manager.get_tools()
        result = await manager.call_tool("fake", "fake_tool", {"value": "ok"})
        health = manager.health()[0]

        assert created == ["fake"]
        assert [tool.name for tool in tools] == ["mcp__fake__fake_tool"]
        assert result.content == "fake:fake_tool:ok"
        assert health.state == MCPServerState.READY
    finally:
        await manager.close()


def test_stdio_is_default_transport_for_existing_configs():
    config = MCPServerConfig(name="demo", command=sys.executable, args=[str(FIXTURE)])

    assert config.transport == MCPTransport.STDIO
    assert create_mcp_client(config).config is config


async def test_unsupported_transport_is_observable_and_does_not_escape_get_tools():
    manager = MCPManager(
        [
            MCPServerConfig(
                name="remote-logs",
                transport=MCPTransport.HTTP,
                url="https://logs.example.invalid/mcp",
            )
        ],
        tool_wait_timeout=0.1,
        retry_initial_delay=0.0,
    )

    try:
        tools = await manager.get_tools()
        health = manager.health()[0]

        assert tools == []
        assert health.name == "remote-logs"
        assert health.state == MCPServerState.FAILED
        assert health.error is not None
        assert "not implemented" in health.error
        assert health.next_retry_at is None
    finally:
        await manager.close()


async def test_start_keeps_fail_fast_for_unsupported_transport():
    manager = MCPManager(
        [
            MCPServerConfig(
                name="remote-logs",
                transport=MCPTransport.SSE,
                url="https://logs.example.invalid/mcp",
            )
        ]
    )

    try:
        try:
            await manager.start()
        except MCPTransportUnsupportedError:
            health = manager.health()[0]
            assert health.state == MCPServerState.FAILED
            assert health.error is not None
        else:
            raise AssertionError("manager.start() should fail fast for unsupported MCP")
    finally:
        await manager.close()


async def test_disabled_server_is_visible_but_never_started():
    called = False

    def factory(config: MCPServerConfig) -> FakeMCPClient:
        nonlocal called
        called = True
        return FakeMCPClient(config)

    manager = MCPManager(
        [
            MCPServerConfig(
                name="disabled",
                command="unused",
                disabled=True,
            )
        ],
        client_factory=factory,
        tool_wait_timeout=0.1,
    )

    try:
        tools = await manager.get_tools()
        health = manager.health()[0]

        assert tools == []
        assert called is False
        assert health.state == MCPServerState.DISABLED
        assert health.tool_count == 0
    finally:
        await manager.close()


async def test_ready_tools_snapshot_does_not_start_disconnected_server():
    """缓存快照只能读取已 READY 工具,不能偷偷启动 MCP 连接。"""
    created: list[str] = []

    def factory(config: MCPServerConfig) -> FakeMCPClient:
        created.append(config.name)
        return FakeMCPClient(config)

    manager = MCPManager(
        [MCPServerConfig(name="lazy", command="unused")],
        client_factory=factory,
        tool_wait_timeout=0.1,
    )

    try:
        tools = await manager.get_ready_tools()
        health = manager.health()[0]

        assert tools == []
        assert created == []
        assert health.state == MCPServerState.DISCONNECTED
    finally:
        await manager.close()


async def test_stdio_transport_does_not_inherit_undeclared_environment(
    monkeypatch,
):
    """删除 SDK 的安全环境边界会把宿主机秘密变量泄漏给 MCP server。"""
    secret_name = "LOOP_ENGINEER_TRANSPORT_UNDECLARED_SECRET"
    monkeypatch.setenv(secret_name, "must-not-leak")
    manager = MCPManager(
        [
            MCPServerConfig(
                name="demo",
                command=sys.executable,
                args=[str(FIXTURE)],
                env={"MCP_DEMO_ENABLE_ENV_TOOL": "1"},
            )
        ]
    )

    try:
        await manager.start()
        result = await manager.call_tool("demo", "read_env", {"name": secret_name})

        assert result.content == ""
    finally:
        await manager.close()


async def test_stdio_transport_inherits_explicit_server_environment():
    """删除 config.env 合并会让明确配置给 server 的凭据和路径失效。"""
    configured_name = "LOOP_ENGINEER_TRANSPORT_CONFIGURED_VALUE"
    manager = MCPManager(
        [
            MCPServerConfig(
                name="demo",
                command=sys.executable,
                args=[str(FIXTURE)],
                env={
                    "MCP_DEMO_ENABLE_ENV_TOOL": "1",
                    configured_name: "configured",
                },
            )
        ]
    )

    try:
        await manager.start()
        result = await manager.call_tool(
            "demo", "read_env", {"name": configured_name}
        )

        assert result.content == "configured"
    finally:
        await manager.close()
