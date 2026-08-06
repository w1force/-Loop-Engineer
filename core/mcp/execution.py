"""MCP tool execution policy.

对应 CCB `packages/mcp-client/src/execution.ts`:本层只管一次 tools/call
的外层超时、心跳和结果错误提升,不管理 server 生命周期和 JSON-RPC 路由。
"""
from __future__ import annotations

import asyncio
import logging
import time

from .client_protocol import MCPClientProtocol
from .errors import MCPToolTimeoutError
from .types import MCPProgressEvent, MCPToolCallOptions, MCPToolResult

logger = logging.getLogger(__name__)


async def call_mcp_tool(
    *,
    client: MCPClientProtocol,
    server_name: str,
    tool_name: str,
    arguments: dict,
    options: MCPToolCallOptions,
) -> MCPToolResult:
    """按统一 MCP 长任务策略调用一个工具。"""

    started_at = time.monotonic()
    saw_server_progress = False

    def _on_progress(event: MCPProgressEvent) -> None:
        nonlocal saw_server_progress
        if event.source == "server":
            saw_server_progress = True
        if options.progress_callback is None:
            return
        try:
            options.progress_callback(event)
        except Exception:
            logger.debug(
                "MCP progress callback failed for %s.%s",
                server_name,
                tool_name,
                exc_info=True,
            )

    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(
            server_name=server_name,
            tool_name=tool_name,
            heartbeat_seconds=options.timeout_seconds
            if options.heartbeat_seconds is None
            else options.heartbeat_seconds,
            started_at=started_at,
            saw_server_progress=lambda: saw_server_progress,
            progress_callback=_on_progress,
        )
    )
    call_options = MCPToolCallOptions(
        timeout_seconds=options.timeout_seconds,
        abort_signal=options.abort_signal,
        progress_callback=_on_progress,
    )
    call_task = asyncio.create_task(
        client.call_tool(tool_name, arguments, options=call_options)
    )
    timeout_task = asyncio.create_task(asyncio.sleep(options.timeout_seconds))

    try:
        done, pending = await asyncio.wait(
            {call_task, timeout_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if timeout_task in done:
            call_task.cancel("Request timed out")
            await asyncio.gather(call_task, return_exceptions=True)
            raise MCPToolTimeoutError(
                server_name, tool_name, options.timeout_seconds
            )
        result = await call_task
        if result.is_error:
            raise RuntimeError(result.content or "MCP tool returned error")
        return result
    finally:
        timeout_task.cancel()
        heartbeat_task.cancel()
        await asyncio.gather(timeout_task, heartbeat_task, return_exceptions=True)


async def _heartbeat_loop(
    *,
    server_name: str,
    tool_name: str,
    heartbeat_seconds: float,
    started_at: float,
    saw_server_progress,
    progress_callback,
) -> None:
    while True:
        await asyncio.sleep(heartbeat_seconds)
        elapsed = time.monotonic() - started_at
        logger.debug(
            "[%s] Tool '%s' still running (%ds elapsed)",
            server_name,
            tool_name,
            int(elapsed),
        )
        progress_callback(
            MCPProgressEvent(
                message="MCP tool still running",
                source="heartbeat",
                elapsed_seconds=elapsed,
                received_server_progress=saw_server_progress(),
                progress=None,
                total=None,
            )
        )
