"""MCP transport client 工厂。"""
from __future__ import annotations

from .client import StdioMCPClient
from .client_protocol import MCPClientProtocol
from .errors import MCPConfigError, MCPTransportUnsupportedError
from .types import MCPServerConfig, MCPTransport


def create_mcp_client(config: MCPServerConfig) -> MCPClientProtocol:
    """根据 MCPServerConfig 创建具体 MCP client。"""

    if config.transport == MCPTransport.STDIO:
        if not config.command:
            raise MCPConfigError(
                f"MCP stdio server '{config.name}' requires a non-empty command"
            )
        return StdioMCPClient(config)

    raise MCPTransportUnsupportedError(config.name, config.transport.value)
