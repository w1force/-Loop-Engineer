"""MCP server 管理器。"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from .client import StdioMCPClient
from .result_policy import MCPResultPolicy
from .tool_adapter import create_mcp_tool
from .types import (
    MCPServerConfig,
    MCPServerHealth,
    MCPServerState,
    MCPToolResult,
    MCPToolSpec,
)


class MCPManager:
    """管理多个 stdio MCP server,并把它们暴露为本项目 Tool。"""

    def __init__(
        self,
        configs: list[MCPServerConfig],
        *,
        tool_wait_timeout: float = 0.0,
        retry_initial_delay: float = 1.0,
        retry_max_delay: float = 30.0,
        clock: Callable[[], float] | None = None,
        result_policy: MCPResultPolicy | None = None,
    ):
        self._clients = {cfg.name: StdioMCPClient(cfg) for cfg in configs}
        self._tool_wait_timeout = tool_wait_timeout
        self._retry_initial_delay = retry_initial_delay
        self._retry_max_delay = retry_max_delay
        self._clock = clock or time.monotonic
        self._result_policy = result_policy or MCPResultPolicy()
        self._states = {
            cfg.name: MCPServerState.DISCONNECTED for cfg in configs
        }
        self._errors: dict[str, str | None] = {cfg.name: None for cfg in configs}
        self._tool_cache: dict[str, list[MCPToolSpec]] = {
            cfg.name: [] for cfg in configs
        }
        self._connect_tasks: dict[str, asyncio.Task[None]] = {}
        self._failure_counts: dict[str, int] = {cfg.name: 0 for cfg in configs}
        self._last_attempt_at: dict[str, float | None] = {
            cfg.name: None for cfg in configs
        }
        self._last_success_at: dict[str, float | None] = {
            cfg.name: None for cfg in configs
        }
        self._next_retry_at: dict[str, float | None] = {
            cfg.name: None for cfg in configs
        }

    async def start(self) -> None:
        """阻塞式启动所有 MCP server。

        这是给测试/脚本/必须依赖 MCP 的入口用的 fail-fast 路径:失败会先写入
        health,再继续抛给调用方。agent 主流程请走 get_tools() 的缓存路径。
        """
        for server_name in self._clients:
            await self._connect_one(server_name, fail_fast=True)

    async def close(self) -> None:
        tasks = [task for task in self._connect_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connect_tasks.clear()
        for name, client in self._clients.items():
            await client.close()
            if self._states[name] == MCPServerState.CONNECTING:
                self._states[name] = MCPServerState.DISCONNECTED

    async def list_tools(self) -> list[MCPToolSpec]:
        """阻塞式实时查询 MCP server 工具列表。"""
        specs: list[MCPToolSpec] = []
        for client in self._clients.values():
            specs.extend(await client.list_tools())
        return specs

    async def start_background(self) -> None:
        for server_name in self._clients:
            task = self._connect_tasks.get(server_name)
            if task is not None and not task.done():
                continue
            if not self._should_start_background_connect(server_name):
                continue
            self._states[server_name] = MCPServerState.CONNECTING
            self._connect_tasks[server_name] = asyncio.create_task(
                self._connect_one(server_name, fail_fast=False)
            )

    async def get_tools(self):
        # 对齐 Claude Code 的主线语义:慢 MCP 不阻塞第一轮,已连接的工具进入工具池,
        # 未连接的 server 留给后台继续准备,下一轮再可见。
        await self.start_background()
        await self._wait_for_background_tools()
        specs = [
            spec
            for server_name in sorted(self._tool_cache)
            for spec in self._tool_cache[server_name]
        ]
        return [
            create_mcp_tool(spec, self, result_policy=self._result_policy)
            for spec in specs
        ]

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict,
        *,
        progress_callback=None,
    ) -> MCPToolResult:
        # tool_adapter 保留原始 server/tool 名到 mcp_info,所以执行时不需要再从
        # mcp__server__tool 字符串反解析,也避免归一化名称和原始名称混淆。
        client = self._clients.get(server_name)
        if client is None:
            raise ValueError(f"未知 MCP server: {server_name}")
        return await client.call_tool(
            tool_name,
            arguments,
            progress_callback=progress_callback,
        )

    def health(self) -> list[MCPServerHealth]:
        return [
            MCPServerHealth(
                name=name,
                state=self._states[name],
                error=self._errors[name],
                tool_count=len(self._tool_cache[name]),
                failure_count=self._failure_counts[name],
                last_attempt_at=self._last_attempt_at[name],
                last_success_at=self._last_success_at[name],
                next_retry_at=self._next_retry_at[name],
            )
            for name in sorted(self._clients)
        ]

    async def _wait_for_background_tools(self) -> None:
        if self._tool_wait_timeout <= 0:
            return
        pending = [task for task in self._connect_tasks.values() if not task.done()]
        if not pending:
            return
        done, _ = await asyncio.wait(pending, timeout=self._tool_wait_timeout)
        for task in done:
            await task

    async def _connect_one(self, server_name: str, *, fail_fast: bool) -> None:
        client = self._clients[server_name]
        self._states[server_name] = MCPServerState.CONNECTING
        self._errors[server_name] = None
        self._last_attempt_at[server_name] = self._clock()
        try:
            await client.start()
            specs = await client.list_tools()
        except asyncio.CancelledError:
            self._states[server_name] = MCPServerState.DISCONNECTED
            raise
        except Exception as exc:
            self._states[server_name] = MCPServerState.FAILED
            self._errors[server_name] = str(exc)
            self._tool_cache[server_name] = []
            self._schedule_retry(server_name)
            await client.close()
            if fail_fast:
                raise
            return
        self._tool_cache[server_name] = specs
        self._failure_counts[server_name] = 0
        self._next_retry_at[server_name] = None
        self._last_success_at[server_name] = self._clock()
        self._states[server_name] = MCPServerState.READY

    def _should_start_background_connect(self, server_name: str) -> bool:
        state = self._states[server_name]
        if state == MCPServerState.DISCONNECTED:
            return True
        if state != MCPServerState.FAILED:
            return False
        next_retry_at = self._next_retry_at[server_name]
        return next_retry_at is not None and self._clock() >= next_retry_at

    def _schedule_retry(self, server_name: str) -> None:
        self._failure_counts[server_name] += 1
        delay = self._next_retry_delay(self._failure_counts[server_name])
        self._next_retry_at[server_name] = self._clock() + delay

    def _next_retry_delay(self, failure_count: int) -> float:
        delay = self._retry_initial_delay * (2 ** max(failure_count - 1, 0))
        return min(delay, self._retry_max_delay)
