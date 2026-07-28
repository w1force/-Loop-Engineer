"""单个 LSP server 实例的生命周期。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from .client import LSPClient
from .config import LSPServerConfig

LSPServerState = Literal["stopped", "starting", "running", "stopping", "error"]


class LSPServerInstance:
    def __init__(self, config: LSPServerConfig):
        self.name = config.name
        self.config = config
        self.state: LSPServerState = "stopped"
        self.last_error: Exception | None = None
        self._start_lock = asyncio.Lock()
        self.client = LSPClient(config.name, self._on_crash)

    def _on_crash(self, error: Exception) -> None:
        self.state = "error"
        self.last_error = error

    async def start(self) -> None:
        async with self._start_lock:
            if self.state == "running":
                return
            self.state = "starting"
            try:
                await self.client.start(
                    self.config.command,
                    self.config.args,
                    env=self.config.env,
                    cwd=self.config.workspace_folder,
                )
                workspace = Path(self.config.workspace_folder).resolve()
                params: dict[str, object] = {
                    "processId": __import__("os").getpid(),
                    "initializationOptions": self.config.initialization_options,
                    "workspaceFolders": [
                        {"uri": workspace.as_uri(), "name": workspace.name}
                    ],
                    "rootPath": str(workspace),
                    "rootUri": workspace.as_uri(),
                    "capabilities": {
                        "workspace": {
                            "configuration": False,
                            "workspaceFolders": False,
                        },
                        "textDocument": {
                            "synchronization": {
                                "dynamicRegistration": False,
                                "willSave": False,
                                "willSaveWaitUntil": False,
                                "didSave": True,
                            },
                            "hover": {
                                "dynamicRegistration": False,
                                "contentFormat": ["markdown", "plaintext"],
                            },
                            "definition": {
                                "dynamicRegistration": False,
                                "linkSupport": True,
                            },
                            "references": {"dynamicRegistration": False},
                            "documentSymbol": {
                                "dynamicRegistration": False,
                                "hierarchicalDocumentSymbolSupport": True,
                            },
                            "callHierarchy": {"dynamicRegistration": False},
                        },
                        "general": {"positionEncodings": ["utf-16"]},
                    },
                }
                await asyncio.wait_for(
                    self.client.initialize(params),
                    timeout=self.config.startup_timeout,
                )
                self.state = "running"
                self.last_error = None
            except Exception as error:
                self.state = "error"
                self.last_error = error
                await self.client.stop()
                raise

    async def stop(self) -> None:
        if self.state in ("stopped", "stopping"):
            return
        self.state = "stopping"
        try:
            await self.client.stop()
            self.state = "stopped"
        except Exception as error:
            self.state = "error"
            self.last_error = error
            raise

    async def send_request(self, method: str, params: object) -> object:
        if self.state != "running" or not self.client.is_initialized:
            raise RuntimeError(f"LSP server '{self.name}' is not healthy")
        return await self.client.send_request(method, params)

    async def send_notification(self, method: str, params: object) -> None:
        if self.state != "running" or not self.client.is_initialized:
            raise RuntimeError(f"LSP server '{self.name}' is not healthy")
        await self.client.send_notification(method, params)

    def on_request(self, method: str, handler) -> None:
        self.client.on_request(method, handler)

    def on_notification(self, method: str, handler) -> None:
        self.client.on_notification(method, handler)
