"""多语言服务器管理、按扩展名路由和文档同步。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import LSPServerConfig
from .server import LSPServerInstance

logger = logging.getLogger("lsp.sync")


class LSPServerManager:
    def __init__(self, configs: list[LSPServerConfig]):
        self._servers = {
            config.name: LSPServerInstance(config) for config in configs
        }
        self._extension_map: dict[str, str] = {}
        for config in configs:
            for extension in config.extension_to_language:
                self._extension_map.setdefault(extension.lower(), config.name)
        self._opened_files: dict[str, str] = {}
        self._versions: dict[str, int] = {}
        self._sync_tasks: set[asyncio.Task[None]] = set()
        for server in self._servers.values():
            server.on_request(
                "workspace/configuration",
                lambda params: [
                    None
                    for _ in (
                        params.get("items", [])
                        if isinstance(params, dict)
                        else []
                    )
                ],
            )

    def get_server_for_file(self, file_path: str) -> LSPServerInstance | None:
        name = self._extension_map.get(Path(file_path).suffix.lower())
        return self._servers.get(name) if name else None

    def get_all_servers(self) -> dict[str, LSPServerInstance]:
        return dict(self._servers)

    async def ensure_server_started(
        self, file_path: str
    ) -> LSPServerInstance | None:
        server = self.get_server_for_file(file_path)
        if server is None:
            return None
        if server.state == "error":
            raise RuntimeError(
                f"LSP server '{server.name}' is unavailable: {server.last_error}"
            )
        if server.state == "stopped":
            await server.start()
        return server

    async def send_request(
        self, file_path: str, method: str, params: object
    ) -> object | None:
        server = await self.ensure_server_started(file_path)
        if server is None:
            return None
        return await server.send_request(method, params)

    def is_file_open(self, file_path: str) -> bool:
        return Path(file_path).resolve().as_uri() in self._opened_files

    async def open_file(self, file_path: str, content: str) -> None:
        server = await self.ensure_server_started(file_path)
        if server is None:
            return
        path = Path(file_path).resolve()
        uri = path.as_uri()
        if self._opened_files.get(uri) == server.name:
            return
        language_id = server.config.extension_to_language.get(
            path.suffix.lower(), "plaintext"
        )
        await server.send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": content,
                }
            },
        )
        self._opened_files[uri] = server.name
        self._versions[uri] = 1

    async def change_file(self, file_path: str, content: str) -> None:
        server = self.get_server_for_file(file_path)
        if server is None:
            return
        path = Path(file_path).resolve()
        uri = path.as_uri()
        if server.state != "running" or self._opened_files.get(uri) != server.name:
            await self.open_file(file_path, content)
            return
        version = self._versions.get(uri, 1) + 1
        await server.send_notification(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": content}],
            },
        )
        self._versions[uri] = version

    async def save_file(self, file_path: str) -> None:
        server = self.get_server_for_file(file_path)
        if server is None or server.state != "running":
            return
        await server.send_notification(
            "textDocument/didSave",
            {"textDocument": {"uri": Path(file_path).resolve().as_uri()}},
        )

    def notify_file_changed(self, file_path: str, content: str) -> None:
        """非阻塞同步文件状态；失败不影响已完成的 Edit/Write。"""
        if self.get_server_for_file(file_path) is None:
            return
        task = asyncio.create_task(self._sync_file(file_path, content))
        self._sync_tasks.add(task)
        task.add_done_callback(self._sync_tasks.discard)

    async def _sync_file(self, file_path: str, content: str) -> None:
        try:
            await self.change_file(file_path, content)
            await self.save_file(file_path)
        except Exception:
            logger.warning("LSP file sync failed for %s", file_path, exc_info=True)

    async def close_all_files(self) -> None:
        entries = list(self._opened_files.items())
        self._opened_files.clear()
        self._versions.clear()
        for uri, server_name in entries:
            server = self._servers.get(server_name)
            if server is None or server.state != "running":
                continue
            try:
                await server.send_notification(
                    "textDocument/didClose", {"textDocument": {"uri": uri}}
                )
            except Exception:
                pass

    async def shutdown(self) -> None:
        if self._sync_tasks:
            await asyncio.gather(*self._sync_tasks, return_exceptions=True)
        await self.close_all_files()
        await asyncio.gather(
            *(
                server.stop()
                for server in self._servers.values()
                if server.state in ("running", "error")
            ),
            return_exceptions=True,
        )


def create_lsp_server_manager(
    configs: list[LSPServerConfig],
) -> LSPServerManager:
    manager = LSPServerManager(configs)
    # 与 CC manager 初始化完成后的 registerLSPNotificationHandlers 对齐。
    from .passive_feedback import register_lsp_notification_handlers

    register_lsp_notification_handlers(manager)
    return manager
