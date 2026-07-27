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
    """一个本地 stdio MCP server 的启动配置。"""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    # env 只叠加到当前进程环境上,便于给某个 MCP server 单独传 token/path。
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 10.0


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
