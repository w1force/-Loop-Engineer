"""最小 stdio MCP client。

只实现基础工具闭环: initialize -> tools/list -> tools/call。
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any

from .errors import MCPConfigError
from .types import MCPProgressEvent, MCPServerConfig, MCPToolResult, MCPToolSpec

MCP_PROTOCOL_VERSION = "2024-11-05"
ProgressCallback = Callable[[MCPProgressEvent], None]


class StdioMCPClient:
    """一个 MCP server 的 stdio 连接。

    第一版只支持本地 stdio,因为它最适合测试和可控接入;HTTP/SSE、OAuth、
    session 续期这些复杂能力后续可以在这个类旁边扩展,不影响 Tool 适配层。
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = ""
        self._next_id = 1
        # MCP stdio 是一条 stdin/stdout 管道。这里先串行 request,避免多个协程
        # 同时读 stdout 导致响应被错误消费。
        self._lock = asyncio.Lock()
        self.capabilities: dict[str, Any] = {}

    async def start(self) -> None:
        if self._proc is not None:
            return
        if not self.config.command:
            raise MCPConfigError(
                f"MCP stdio server '{self.config.name}' requires a non-empty command"
            )
        env = os.environ.copy()
        env.update(self.config.env)
        self._proc = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc))
        result = await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "loop-engineer", "version": "0.1.0"},
            },
        )
        self.capabilities = dict(result.get("capabilities") or {})
        # MCP 规范里 initialized 是 notification,没有 id/response。
        await self._notify("notifications/initialized", {})

    async def list_tools(self) -> list[MCPToolSpec]:
        self._ensure_started()
        # capabilities.tools 在协议里常见形状是 {},空 dict 也表示支持 tools。
        if self.capabilities and "tools" not in self.capabilities:
            return []
        result = await self._request("tools/list", {})
        specs: list[MCPToolSpec] = []
        for raw in result.get("tools") or []:
            specs.append(
                MCPToolSpec(
                    server_name=self.config.name,
                    name=str(raw.get("name") or ""),
                    description=str(raw.get("description") or ""),
                    input_schema=dict(raw.get("inputSchema") or {"type": "object"}),
                    annotations=dict(raw.get("annotations") or {}),
                )
            )
        return [s for s in specs if s.name]

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> MCPToolResult:
        self._ensure_started()
        progress_events: list[MCPProgressEvent] = []
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
            progress_callback=progress_callback,
            progress_events=progress_events,
        )
        return _to_tool_result(result, progress_events)

    async def close(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if self._stderr_task is not None:
            await self._stderr_task
            self._stderr_task = None

    def _ensure_started(self) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not started")

    async def _notify(self, method: str, params: dict) -> None:
        self._ensure_started()
        assert self._proc is not None and self._proc.stdin is not None
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self._proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        await self._proc.stdin.drain()

    async def _request(
        self,
        method: str,
        params: dict,
        *,
        progress_callback: ProgressCallback | None = None,
        progress_events: list[MCPProgressEvent] | None = None,
    ) -> dict:
        self._ensure_started() if self._proc is not None else None
        async with self._lock:
            req_id = self._next_id
            self._next_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }
            assert self._proc is not None
            assert self._proc.stdin is not None and self._proc.stdout is not None
            self._proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            await self._proc.stdin.drain()
            while True:
                # 暂时只处理 line-delimited JSON-RPC。测试 server 和常见 stdio MCP
                # 都使用这种形状;如果后续接 Content-Length framing,在这里扩展即可。
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=self.config.timeout
                )
                if not line:
                    detail = _format_stderr_tail(self._stderr_tail)
                    raise RuntimeError(
                        f"MCP server '{self.config.name}' closed stdout{detail}"
                    )
                response = json.loads(line.decode("utf-8"))
                if response.get("method") == "notifications/progress":
                    event = _parse_progress(dict(response.get("params") or {}))
                    if progress_events is not None:
                        progress_events.append(event)
                    if progress_callback is not None:
                        try:
                            progress_callback(event)
                        except Exception:
                            pass
                    continue
                if response.get("id") != req_id:
                    continue
                if "error" in response:
                    err = response["error"]
                    raise RuntimeError(err.get("message") or str(err))
                return dict(response.get("result") or {})

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            self._stderr_tail = (self._stderr_tail + text)[-65536:]


def _to_tool_result(
    result: dict,
    progress_events: list[MCPProgressEvent],
) -> MCPToolResult:
    return MCPToolResult(
        content=_format_content_for_model(result),
        is_error=bool(result.get("isError", False)),
        raw_content=result.get("content"),
        structured_content=result.get("structuredContent"),
        progress=list(progress_events),
    )


def _format_content_for_model(result: dict) -> str:
    """把 MCP ToolResult 收窄成本项目 ToolResultBlock 能承载的字符串。"""
    content = result.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        text = "\n".join(parts)
        if text:
            return text
    if "structuredContent" in result:
        return json.dumps(result["structuredContent"], ensure_ascii=False, sort_keys=True)
    return ""


def _parse_progress(params: dict) -> MCPProgressEvent:
    return MCPProgressEvent(
        progress_token=params.get("progressToken"),
        progress=params.get("progress"),
        total=params.get("total"),
        message=params.get("message"),
    )


def _format_stderr_tail(stderr_tail: str) -> str:
    text = stderr_tail.strip()
    if not text:
        return ""
    return f"; stderr tail: {text[-2000:]}"
