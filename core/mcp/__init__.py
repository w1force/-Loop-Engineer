"""MCP 基础框架入口。"""
from .client import StdioMCPClient
from .config_loader import load_mcp_configs, load_mcp_configs_from_file
from .errors import MCPConfigError, MCPError, MCPTransportUnsupportedError
from .factory import create_mcp_client
from .manager import MCPManager
from .presets import build_tda_mcp_config, extract_tda_thread_dump_from_zip
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
    "build_tda_mcp_config",
    "create_mcp_tool",
    "create_mcp_client",
    "extract_tda_thread_dump_from_zip",
    "load_mcp_configs",
    "load_mcp_configs_from_file",
    "mcp_info_from_string",
    "normalize_name_for_mcp",
]
