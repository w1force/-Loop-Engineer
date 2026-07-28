"""MCP transport client 协议。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .types import MCPProgressEvent, MCPServerConfig, MCPToolResult, MCPToolSpec

ProgressCallback = Callable[[MCPProgressEvent], None]


class MCPClientProtocol(Protocol):
    """所有 MCP transport client 需要实现的最小稳定接口。"""

    config: MCPServerConfig

    async def start(self) -> None:
        """建立连接并完成 MCP initialize。"""

    async def list_tools(self) -> list[MCPToolSpec]:
        """返回当前 server 暴露的 MCP tools。"""

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> MCPToolResult:
        """调用 MCP server 上的原始 tool name。"""

    async def close(self) -> None:
        """关闭连接并释放 transport 资源。"""
