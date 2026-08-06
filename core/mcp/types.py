"""MCP 基础数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MCPServerState(str, Enum):
    """一个 MCP server 在 manager 里的生命周期状态。"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    FAILED = "failed"
    NEEDS_AUTH = "needs_auth"
    DISABLED = "disabled"


class MCPTransport(str, Enum):
    """MCP server 的连接方式。

    目前只有 stdio 有真实 client。其余值先作为正式扩展入口保留,避免
    manager/tool_adapter 继续和 stdio 写死在一起。
    """

    STDIO = "stdio"
    SSE = "sse"
    SSE_IDE = "sse-ide"
    HTTP = "http"
    WS = "ws"
    SDK = "sdk"
    CLAUDEAI_PROXY = "claudeai-proxy"


@dataclass(frozen=True)
class MCPServerHealth:
    """给调用方观察 MCP server 当前状态的轻量快照。"""

    name: str
    state: MCPServerState
    error: str | None = None
    tool_count: int = 0
    failure_count: int = 0
    last_attempt_at: float | None = None
    last_success_at: float | None = None
    next_retry_at: float | None = None


@dataclass(frozen=True)
class MCPServerConfig:
    """一个 MCP server 的连接配置。

    stdio 是当前唯一真实实现;url/headers/oauth 是远程 transport 的长期扩展口。
    """

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    # env 只显式传给该 server;官方 SDK 另外保留 PATH/HOME 等安全基础环境,
    # 不会把宿主进程的全部 token/secret 隐式泄漏给子进程。
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 10.0
    transport: MCPTransport = MCPTransport.STDIO
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    oauth: dict[str, Any] | None = None
    disabled: bool = False


@dataclass(frozen=True)
class MCPToolExecutionPolicy:
    """MCP tools/call 的统一执行策略。

    对齐 CCB:工具调用超时独立于 initialize/tools/list 等控制请求超时。
    """

    timeout_seconds: float = 100_000.0
    heartbeat_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("MCP tool timeout must be greater than 0")
        if self.heartbeat_seconds <= 0:
            raise ValueError("MCP tool heartbeat must be greater than 0")


@dataclass(frozen=True)
class MCPToolSpec:
    """MCP server 暴露的一个工具。"""

    server_name: str
    name: str
    description: str
    input_schema: dict
    annotations: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MCPProgressEvent:
    """MCP progress notification 的轻量快照。"""

    progress_token: str | int | None = None
    progress: int | float | None = None
    total: int | float | None = None
    message: str | None = None
    source: str = "server"
    elapsed_seconds: float | None = None
    received_server_progress: bool | None = None


@dataclass(frozen=True)
class MCPToolCallOptions:
    """一次 MCP tools/call 的运行时选项。

    execution 层负责填充这些值;transport 只按选项执行当前 request。
    """

    timeout_seconds: float
    heartbeat_seconds: float = 30.0
    abort_signal: Any | None = None
    progress_callback: Any | None = None


@dataclass(frozen=True)
class MCPToolResult:
    """MCP tools/call 的简化结果。"""

    content: str
    is_error: bool = False
    raw_content: Any | None = None
    structured_content: Any | None = None
    progress: list[MCPProgressEvent] = field(default_factory=list)
    truncated: bool = False
    artifact_path: str | None = None
    original_chars: int | None = None
