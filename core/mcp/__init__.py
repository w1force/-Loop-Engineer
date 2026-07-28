"""MCP 基础框架入口。"""
from .client import StdioMCPClient
from .errors import MCPConfigError, MCPError, MCPTransportUnsupportedError
from .factory import create_mcp_client
from .manager import MCPManager
from .result_policy import MCPResultPolicy
from .strings import build_mcp_tool_name, mcp_info_from_string, normalize_name_for_mcp
from .tool_adapter import create_mcp_tool
from .types import (
    MCPProgressEvent,
    MCPServerConfig,
    MCPServerHealth,
    MCPServerState,
    MCPToolResult,
    MCPToolSpec,
    MCPTransport,
)

__all__ = [
    "MCPConfigError",
    "MCPError",
    "MCPManager",
    "MCPProgressEvent",
    "MCPResultPolicy",
    "MCPServerConfig",
    "MCPServerHealth",
    "MCPServerState",
    "MCPToolResult",
    "MCPToolSpec",
    "MCPTransport",
    "MCPTransportUnsupportedError",
    "StdioMCPClient",
    "build_mcp_tool_name",
    "create_mcp_tool",
    "create_mcp_client",
    "mcp_info_from_string",
    "normalize_name_for_mcp",
]
