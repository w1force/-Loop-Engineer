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


class MCPToolTimeoutError(MCPError):
    """单次 MCP 工具调用超过工具执行超时。"""

    def __init__(self, server_name: str, tool_name: str, timeout_seconds: float):
        self.server_name = server_name
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"MCP server '{server_name}' tool '{tool_name}' timed out "
            f"after {timeout_seconds:g}s"
        )


class MCPConnectionClosedError(MCPError):
    """MCP transport 已关闭,client 不应继续复用。"""


class MCPProtocolError(MCPError):
    """MCP 协议消息无法被当前 client 正确处理。"""
