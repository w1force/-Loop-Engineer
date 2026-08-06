"""基于官方 Python MCP SDK transport 的 stdio MCP client。"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

import anyio
import mcp.types as sdk_types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.message import SessionMessage

from .errors import (
    MCPConfigError,
    MCPConnectionClosedError,
    MCPProtocolError,
    MCPToolTimeoutError,
)
from .types import (
    MCPProgressEvent,
    MCPServerConfig,
    MCPToolCallOptions,
    MCPToolResult,
    MCPToolSpec,
)

MCP_PROTOCOL_VERSION = "2024-11-05"
ProgressCallback = Callable[[MCPProgressEvent], None]
logger = logging.getLogger(__name__)


@dataclass
class _PendingRequest:
    future: asyncio.Future[dict]
    progress_callback: ProgressCallback | None = None


class StdioMCPClient:
    """一个 MCP server 的 stdio 连接。

    官方 SDK 负责进程、stdio framing 和跨平台进程树清理;本类只维护 CCB
    长任务治理需要的请求路由,不依赖 SDK 私有 request/session 状态。
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._read_stream: Any | None = None
        self._write_stream: Any | None = None
        self._transport_owner_task: asyncio.Task[None] | None = None
        self._transport_close = asyncio.Event()
        self._stderr_file: TextIO | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_tail = ""
        self._next_id = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._write_lock = asyncio.Lock()
        self._terminal_error: BaseException | None = None
        self.capabilities: dict[str, Any] = {}

    async def start(self) -> None:
        if self._transport_owner_task is not None and not self._transport_owner_task.done():
            return
        if not self.config.command:
            raise MCPConfigError(
                f"MCP stdio server '{self.config.name}' requires a non-empty command"
            )
        self._terminal_error = None
        self._transport_close = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._transport_owner_task = asyncio.create_task(self._run_transport(ready))
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=self.config.timeout)
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
        except BaseException:
            await self.close()
            raise

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
        options: MCPToolCallOptions | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> MCPToolResult:
        self._ensure_started()
        progress_events: list[MCPProgressEvent] = []
        if options is None:
            options = MCPToolCallOptions(
                timeout_seconds=self.config.timeout,
                progress_callback=progress_callback,
            )
        else:
            progress_callback = options.progress_callback
        try:
            result = await self._request(
                "tools/call",
                {"name": name, "arguments": arguments},
                timeout=options.timeout_seconds,
                abort_signal=options.abort_signal,
                progress_callback=progress_callback,
                progress_events=progress_events,
            )
        except asyncio.TimeoutError as exc:
            raise MCPToolTimeoutError(
                self.config.name,
                name,
                options.timeout_seconds,
            ) from exc
        return _to_tool_result(result, progress_events)

    async def close(self) -> None:
        owner = self._transport_owner_task
        if owner is None:
            return
        self._transport_close.set()
        await asyncio.gather(owner, return_exceptions=True)
        self._transport_owner_task = None
        self._fail_all_pending(
            MCPConnectionClosedError(f"MCP server '{self.config.name}' closed")
        )

    def _ensure_started(self) -> None:
        if self._read_stream is None or self._write_stream is None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not started")

    async def _notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(
        self,
        method: str,
        params: dict,
        *,
        timeout: float | None = None,
        abort_signal: asyncio.Event | None = None,
        progress_callback: ProgressCallback | None = None,
        progress_events: list[MCPProgressEvent] | None = None,
    ) -> dict:
        self._ensure_started()
        if self._terminal_error is not None:
            raise self._terminal_error
        req_id = self._next_id
        self._next_id += 1
        effective_timeout = timeout if timeout is not None else self.config.timeout
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        if progress_callback is not None:
            payload["params"] = {
                **params,
                "_meta": {
                    **dict((params.get("_meta") if isinstance(params, dict) else {}) or {}),
                    "progressToken": req_id,
                },
            }

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict] = loop.create_future()

        def _store_progress(event: MCPProgressEvent) -> None:
            if progress_events is not None:
                progress_events.append(event)
            if progress_callback is not None:
                try:
                    progress_callback(event)
                except Exception:
                    # 工具 progress 的观测回调不能破坏协议读取循环。
                    logger.debug(
                        "MCP progress callback failed for request %s on server %s",
                        req_id,
                        self.config.name,
                        exc_info=True,
                    )

        self._pending[req_id] = _PendingRequest(
            future=future,
            progress_callback=_store_progress if progress_callback is not None else None,
        )
        waiters: set[asyncio.Task] = set()
        try:
            await self._send(payload)
            waiters = {
                asyncio.create_task(asyncio.wait_for(future, timeout=effective_timeout))
            }
            abort_task: asyncio.Task | None = None
            if abort_signal is not None:
                abort_task = asyncio.create_task(abort_signal.wait())
                waiters.add(abort_task)
            done, pending = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED
            )
            first = next(iter(done))
            if first is abort_task:
                raise asyncio.CancelledError("Request aborted")
            try:
                return dict(first.result() or {})
            except asyncio.TimeoutError:
                await self._cancel_request(req_id, "Request timed out")
                raise
            finally:
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError as exc:
            reason = str(exc) or "Request cancelled"
            await self._cancel_request(req_id, reason)
            for task in waiters:
                task.cancel()
            if waiters:
                await asyncio.gather(*waiters, return_exceptions=True)
            raise
        finally:
            self._pending.pop(req_id, None)

    async def _send(self, payload: dict) -> None:
        self._ensure_started()
        assert self._write_stream is not None
        message = sdk_types.JSONRPCMessage.model_validate(payload)
        async with self._write_lock:
            await self._write_stream.send(SessionMessage(message=message))

    async def _cancel_request(self, req_id: int, reason: str) -> None:
        pending = self._pending.pop(req_id, None)
        if pending is not None and not pending.future.done():
            pending.future.cancel()
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": req_id, "reason": reason},
                }
            )
        except Exception:
            # CCB 的取消通知是 best-effort:本地请求已结束,通知失败只作为连接问题暴露。
            if self._terminal_error is None:
                self._terminal_error = MCPConnectionClosedError(
                    f"MCP server '{self.config.name}' failed while sending cancellation"
                )

    async def _read_loop(self) -> None:
        assert self._read_stream is not None
        try:
            async for session_message in self._read_stream:
                if isinstance(session_message, Exception):
                    raise MCPProtocolError(
                        f"MCP server '{self.config.name}' sent an invalid message: "
                        f"{session_message}"
                    )
                response = session_message.message.root.model_dump(
                    by_alias=True,
                    mode="json",
                    exclude_none=True,
                )
                self._route_message(response)
        except asyncio.CancelledError:
            raise
        except (anyio.EndOfStream, anyio.ClosedResourceError) as exc:
            error = self._connection_error("closed stdout", exc)
            self._mark_terminal(error)
        except Exception as exc:
            error = exc
            if not isinstance(exc, (MCPConnectionClosedError, MCPProtocolError)):
                error = self._connection_error("transport failed", exc)
            self._mark_terminal(error)
        else:
            if not self._transport_close.is_set():
                self._mark_terminal(self._connection_error("closed stdout"))

    def _route_message(self, response: dict) -> None:
        if response.get("method") == "notifications/progress":
            params = dict(response.get("params") or {})
            token = params.get("progressToken")
            pending = self._pending.get(token) if token is not None else None
            if pending is None or pending.progress_callback is None:
                logger.debug(
                    "Discarding late or unknown MCP progress for server %s token %r",
                    self.config.name,
                    token,
                )
                return
            pending.progress_callback(_parse_progress(params))
            return
        if "id" not in response:
            return
        req_id = response["id"]
        pending = self._pending.get(req_id)
        if pending is None or pending.future.done():
            logger.debug(
                "Discarding late or unknown MCP response for server %s request %r",
                self.config.name,
                req_id,
            )
            return
        if "error" in response:
            err = response["error"]
            pending.future.set_exception(RuntimeError(err.get("message") or str(err)))
            return
        pending.future.set_result(dict(response.get("result") or {}))

    def _fail_all_pending(self, error: BaseException) -> None:
        for req_id, pending in list(self._pending.items()):
            if not pending.future.done():
                pending.future.set_exception(error)
            self._pending.pop(req_id, None)

    async def _run_transport(self, ready: asyncio.Future[None]) -> None:
        """在同一 task 内持有官方 SDK context,避免跨 task 退出 cancel scope。"""
        stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self._stderr_file = stderr_file
        close_waiter: asyncio.Task[bool] | None = None
        try:
            parameters = StdioServerParameters(
                command=self.config.command,
                args=list(self.config.args),
                env=dict(self.config.env) or None,
                encoding_error_handler="replace",
            )
            async with stdio_client(parameters, errlog=stderr_file) as streams:
                self._read_stream, self._write_stream = streams
                self._reader_task = asyncio.create_task(self._read_loop())
                if not ready.done():
                    ready.set_result(None)

                close_waiter = asyncio.create_task(self._transport_close.wait())
                done, pending = await asyncio.wait(
                    {self._reader_task, close_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if self._reader_task in done and not self._transport_close.is_set():
                    error = self._reader_task.exception()
                    if error is not None:
                        self._mark_terminal(error)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error = self._connection_error("failed to start or maintain transport", exc)
            if not ready.done():
                ready.set_exception(error)
            elif not self._transport_close.is_set():
                self._mark_terminal(error)
        finally:
            if close_waiter is not None and not close_waiter.done():
                close_waiter.cancel()
                await asyncio.gather(close_waiter, return_exceptions=True)
            if self._reader_task is not None and not self._reader_task.done():
                self._reader_task.cancel()
                await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
            self._read_stream = None
            self._write_stream = None
            self._capture_stderr_tail()
            stderr_file.close()
            self._stderr_file = None
            if not ready.done():
                ready.set_exception(self._connection_error("transport stopped"))

    def _capture_stderr_tail(self) -> None:
        if self._stderr_file is None:
            return
        try:
            self._stderr_file.flush()
            self._stderr_file.seek(0, 2)
            size = self._stderr_file.tell()
            self._stderr_file.seek(max(0, size - 65536))
            self._stderr_tail = self._stderr_file.read()[-65536:]
        except (OSError, ValueError):
            return

    def _connection_error(
        self,
        action: str,
        cause: BaseException | None = None,
    ) -> MCPConnectionClosedError:
        self._capture_stderr_tail()
        detail = _format_stderr_tail(self._stderr_tail)
        suffix = f": {cause}" if cause and str(cause) else ""
        return MCPConnectionClosedError(
            f"MCP server '{self.config.name}' {action}{suffix}{detail}"
        )

    def _mark_terminal(self, error: BaseException) -> None:
        if self._terminal_error is None:
            self._terminal_error = error
        self._fail_all_pending(self._terminal_error)


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
                parts.append(_json_dumps(item))
        text = "\n".join(parts)
        if text:
            return text
    if "structuredContent" in result:
        return _json_dumps(result["structuredContent"], sort_keys=True)
    return ""


def _json_dumps(value, *, sort_keys: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=sort_keys)


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
