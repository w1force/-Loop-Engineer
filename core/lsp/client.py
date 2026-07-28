"""基于 stdio 的 LSP JSON-RPC 客户端。

对应 CC ``src/services/lsp/LSPClient.ts``。不实现语言语义，只负责启动外部进程、
JSON-RPC framing、initialize/initialized 握手以及请求分发。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import os
from typing import Any

logger = logging.getLogger("lsp.client")

RequestHandler = Callable[[object], object | Awaitable[object]]
NotificationHandler = Callable[[object], object | Awaitable[object]]


class LSPClient:
    def __init__(
        self,
        server_name: str,
        on_crash: Callable[[Exception], None] | None = None,
    ):
        self.server_name = server_name
        self._on_crash = on_crash
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._request_handlers: dict[str, RequestHandler] = {}
        self._notification_handlers: dict[str, list[NotificationHandler]] = {}
        self._next_id = 0
        self._stopping = False
        self.is_initialized = False

    async def start(
        self,
        command: str,
        args: tuple[str, ...],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        if self._process is not None:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **(env or {})},
                cwd=cwd,
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                f"LSP server '{self.server_name}' command not found: {command}"
            ) from error

        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        self._wait_task = asyncio.create_task(self._wait_for_exit())

    async def initialize(self, params: dict[str, object]) -> dict[str, object]:
        result = await self.send_request("initialize", params, require_initialized=False)
        if not isinstance(result, dict):
            raise RuntimeError(
                f"LSP server '{self.server_name}' returned invalid initialize result"
            )
        await self.send_notification("initialized", {})
        self.is_initialized = True
        return result

    async def send_request(
        self,
        method: str,
        params: object,
        *,
        require_initialized: bool = True,
    ) -> object:
        if self._process is None:
            raise RuntimeError("LSP client not started")
        if require_initialized and not self.is_initialized:
            raise RuntimeError("LSP server not initialized")
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def send_notification(self, method: str, params: object) -> None:
        if self._process is None:
            raise RuntimeError("LSP client not started")
        await self._write_message(
            {"jsonrpc": "2.0", "method": method, "params": params}
        )

    def on_request(self, method: str, handler: RequestHandler) -> None:
        self._request_handlers[method] = handler

    def on_notification(
        self, method: str, handler: NotificationHandler
    ) -> None:
        self._notification_handlers.setdefault(method, []).append(handler)

    async def stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._stopping = True
        try:
            if process.returncode is None and self.is_initialized:
                try:
                    await asyncio.wait_for(
                        self.send_request("shutdown", {}), timeout=2.0
                    )
                    await self.send_notification("exit", {})
                except (Exception, asyncio.TimeoutError):
                    logger.debug("LSP graceful shutdown failed", exc_info=True)
        finally:
            self.is_initialized = False
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            for task in (self._reader_task, self._stderr_task, self._wait_task):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (
                        self._reader_task,
                        self._stderr_task,
                        self._wait_task,
                    )
                    if task is not None and task is not asyncio.current_task()
                ),
                return_exceptions=True,
            )
            self._fail_pending(RuntimeError("LSP client stopped"))
            self._process = None
            self._reader_task = None
            self._stderr_task = None
            self._wait_task = None
            self._stopping = False

    async def _write_message(self, message: dict[str, object]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("LSP server stdin not available")
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
        packet = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        async with self._write_lock:
            process.stdin.write(packet)
            await process.stdin.drain()

    async def _reader_loop(self) -> None:
        try:
            while True:
                message = await self._read_message()
                if message is None:
                    break
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("LSP reader failed for %s", self.server_name)
            self._fail_pending(error)

    async def _read_message(self) -> dict[str, Any] | None:
        process = self._process
        if process is None or process.stdout is None:
            return None
        content_length: int | None = None
        while True:
            line = await process.stdout.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            name, _, value = line.decode("ascii", errors="replace").partition(":")
            if name.lower() == "content-length":
                content_length = int(value.strip())
        if content_length is None:
            raise RuntimeError("LSP message missing Content-Length")
        body = await process.stdout.readexactly(content_length)
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise RuntimeError("LSP message root must be an object")
        return parsed

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message:
            if "id" in message:
                await self._handle_server_request(message)
            else:
                await self._handle_server_notification(message)
            return
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            future.set_exception(
                RuntimeError(
                    f"LSP error {error.get('code', -32603)}: "
                    f"{error.get('message', 'request failed')}"
                )
            )
        else:
            future.set_result(message.get("result"))

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        handler = self._request_handlers.get(str(message["method"]))
        if handler is None:
            await self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {message['method']}",
                    },
                }
            )
            return
        try:
            result = handler(message.get("params"))
            if isinstance(result, Awaitable):
                result = await result
            await self._write_message(
                {"jsonrpc": "2.0", "id": request_id, "result": result}
            )
        except Exception as error:
            await self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": str(error)},
                }
            )

    async def _handle_server_notification(
        self, message: dict[str, Any]
    ) -> None:
        handlers = self._notification_handlers.get(str(message["method"]), [])
        for handler in handlers:
            try:
                result = handler(message.get("params"))
                if isinstance(result, Awaitable):
                    await result
            except Exception:
                logger.warning(
                    "LSP notification handler failed for %s: %s",
                    self.server_name,
                    message["method"],
                    exc_info=True,
                )

    async def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while line := await process.stderr.readline():
            logger.debug(
                "[LSP SERVER %s] %s",
                self.server_name,
                line.decode(errors="replace").rstrip(),
            )

    async def _wait_for_exit(self) -> None:
        process = self._process
        if process is None:
            return
        code = await process.wait()
        self.is_initialized = False
        if not self._stopping:
            error = RuntimeError(
                f"LSP server '{self.server_name}' exited with code {code}"
            )
            self._fail_pending(error)
            if self._on_crash:
                self._on_crash(error)

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
