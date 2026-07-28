"""MCP 专用错误类型。"""
from __future__ import annotations


class MCPError(Exception):
    """MCP 层错误基类。"""


class MCPTransportUnsupportedError(MCPError):
    """配置了某种 transport,但当前还没有对应 client 实现。"""

    def __init__(self, server_name: str, transport: str):
        super().__init__(
            f"MCP server '{server_name}' uses transport '{transport}', "
            "but that transport is not implemented yet"
        )
        self.server_name = server_name
        self.transport = transport


class MCPConfigError(MCPError):
    """MCP server 配置无法用于当前 transport。"""

