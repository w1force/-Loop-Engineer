"""MCP server 管理器。"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from .client_protocol import MCPClientProtocol
from .errors import MCPTransportUnsupportedError
from .factory import create_mcp_client
from .result_policy import MCPResultPolicy
from .tool_adapter import create_mcp_tool
from .types import (
    MCPServerConfig,
    MCPServerHealth,
    MCPServerState,
    MCPToolResult,
    MCPToolSpec,
    MCPTransport,
)

MCPClientFactory = Callable[[MCPServerConfig], MCPClientProtocol]


class MCPManager:
    """管理多个 MCP server,并把它们暴露为本项目 Tool。"""

    def __init__(
        self,
        configs: list[MCPServerConfig],
        *,
        tool_wait_timeout: float = 0.0,
        retry_initial_delay: float = 1.0,
        retry_max_delay: float = 30.0,
        retry_max_attempts: int = 5,
        clock: Callable[[], float] | None = None,
        result_policy: MCPResultPolicy | None = None,
        client_factory: MCPClientFactory | None = None,
    ):
        self._configs = {cfg.name: cfg for cfg in configs}
        self._clients: dict[str, MCPClientProtocol] = {}
        self._client_factory = client_factory or create_mcp_client
        self._tool_wait_timeout = tool_wait_timeout
        self._retry_initial_delay = retry_initial_delay
        self._retry_max_delay = retry_max_delay
        self._retry_max_attempts = retry_max_attempts
        self._clock = clock or time.monotonic
        self._result_policy = result_policy or MCPResultPolicy()
        self._states = {
            cfg.name: (
                MCPServerState.DISABLED
                if cfg.disabled
                else MCPServerState.DISCONNECTED
            )
            for cfg in configs
        }
        self._errors: dict[str, str | None] = {cfg.name: None for cfg in configs}
        self._tool_cache: dict[str, list[MCPToolSpec]] = {
            cfg.name: [] for cfg in configs
        }
        self._connect_tasks: dict[str, asyncio.Task[None]] = {}
        self._retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False
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
        for server_name in self._configs:
            if self._states[server_name] == MCPServerState.DISABLED:
                continue
            await self._connect_one(server_name, fail_fast=True)

    async def close(self) -> None:
        self._closed = True
        tasks = [
            task
            for task in [*self._connect_tasks.values(), *self._retry_tasks.values()]
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connect_tasks.clear()
        self._retry_tasks.clear()
        for name in list(self._clients):
            await self._close_client(name)
            if self._states[name] == MCPServerState.CONNECTING:
                self._states[name] = MCPServerState.DISCONNECTED

    async def list_tools(self) -> list[MCPToolSpec]:
        """阻塞式实时查询 MCP server 工具列表。"""
        specs: list[MCPToolSpec] = []
        for server_name in self._configs:
            if self._states[server_name] == MCPServerState.DISABLED:
                continue
            client = self._get_or_create_client(server_name)
            specs.extend(await client.list_tools())
        return specs

    async def start_background(self) -> None:
        if self._closed:
            return
        for server_name in self._configs:
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

    async def get_ready_tools(self):
        """只读取当前 READY server 的工具缓存,不启动连接、不等待慢 server。

        这个入口给 query_loop 的 refreshTools 使用:连接生命周期由
        start_background/backoff 负责,刷新工具列表时只拿已经发现成功的工具。
        """
        specs = [
            spec
            for server_name in sorted(self._tool_cache)
            if self._states[server_name] == MCPServerState.READY
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
        if server_name not in self._configs:
            raise ValueError(f"未知 MCP server: {server_name}")
        if self._states[server_name] == MCPServerState.DISABLED:
            raise ValueError(f"MCP server '{server_name}' is disabled")
        client = self._get_or_create_client(server_name)
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
            for name in sorted(self._configs)
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
        self._states[server_name] = MCPServerState.CONNECTING
        self._errors[server_name] = None
        self._last_attempt_at[server_name] = self._clock()
        try:
            client = self._get_or_create_client(server_name)
            await client.start()
            specs = await client.list_tools()
        except asyncio.CancelledError:
            self._states[server_name] = MCPServerState.DISCONNECTED
            raise
        except MCPTransportUnsupportedError as exc:
            self._states[server_name] = MCPServerState.FAILED
            self._errors[server_name] = str(exc)
            self._tool_cache[server_name] = []
            self._next_retry_at[server_name] = None
            if fail_fast:
                raise
            return
        except Exception as exc:
            self._states[server_name] = MCPServerState.FAILED
            self._errors[server_name] = str(exc)
            self._tool_cache[server_name] = []
            self._schedule_retry(server_name)
            await self._close_client(server_name)
            if not fail_fast:
                self._schedule_background_retry(server_name)
            if fail_fast:
                raise
            return
        self._tool_cache[server_name] = specs
        self._failure_counts[server_name] = 0
        self._next_retry_at[server_name] = None
        self._last_success_at[server_name] = self._clock()
        self._states[server_name] = MCPServerState.READY

    def _get_or_create_client(self, server_name: str) -> MCPClientProtocol:
        client = self._clients.get(server_name)
        if client is not None:
            return client
        config = self._configs[server_name]
        client = self._client_factory(config)
        self._clients[server_name] = client
        return client

    async def _close_client(self, server_name: str) -> None:
        client = self._clients.pop(server_name, None)
        if client is None:
            return
        await client.close()

    def _should_start_background_connect(self, server_name: str) -> bool:
        state = self._states[server_name]
        if state in {MCPServerState.DISABLED, MCPServerState.NEEDS_AUTH}:
            return False
        if state == MCPServerState.DISCONNECTED:
            return True
        if state != MCPServerState.FAILED:
            return False
        next_retry_at = self._next_retry_at[server_name]
        return next_retry_at is not None and self._clock() >= next_retry_at

    def _schedule_retry(self, server_name: str) -> None:
        self._failure_counts[server_name] += 1
        if self._failure_counts[server_name] >= self._retry_max_attempts:
            self._next_retry_at[server_name] = None
            return
        delay = self._next_retry_delay(self._failure_counts[server_name])
        self._next_retry_at[server_name] = self._clock() + delay

    def _next_retry_delay(self, failure_count: int) -> float:
        delay = self._retry_initial_delay * (2 ** max(failure_count - 1, 0))
        return min(delay, self._retry_max_delay)

    def _supports_background_retry(self, server_name: str) -> bool:
        transport = self._configs[server_name].transport
        return transport not in {
            # 对齐 CCB:stdio 是本地进程,sdk 是内部 client,断开后不默认后台自重连。
            # 如 TDA 未来需要 stdio 自动重启,应显式加业务配置,不能混进通用默认。
            MCPTransport.STDIO,
            MCPTransport.SDK,
        }

    def _schedule_background_retry(self, server_name: str) -> None:
        if self._closed or not self._supports_background_retry(server_name):
            return
        next_retry_at = self._next_retry_at[server_name]
        if next_retry_at is None:
            return
        task = self._retry_tasks.get(server_name)
        if task is not None and not task.done():
            return
        delay = max(next_retry_at - self._clock(), 0.0)
        self._retry_tasks[server_name] = asyncio.create_task(
            self._retry_background_after_delay(server_name, delay)
        )

    async def _retry_background_after_delay(
        self, server_name: str, delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
            if self._closed or not self._should_start_background_connect(server_name):
                return
            current = asyncio.current_task()
            if current is not None:
                self._connect_tasks[server_name] = current
            await self._connect_one(server_name, fail_fast=False)
        except asyncio.CancelledError:
            raise
        finally:
            current = asyncio.current_task()
            if self._connect_tasks.get(server_name) is current:
                self._connect_tasks.pop(server_name, None)
            if self._retry_tasks.get(server_name) is current:
                self._retry_tasks.pop(server_name, None)
