"""Language Server Protocol 客户端。

对齐 Claude Code 的分层:
LSP Tool → LSPServerManager → LSPServerInstance → LSPClient → 外部语言服务器。
"""

from .config import LSPServerConfig, default_lsp_server_configs
from .manager import LSPServerManager, create_lsp_server_manager

__all__ = [
    "LSPServerConfig",
    "LSPServerManager",
    "create_lsp_server_manager",
    "default_lsp_server_configs",
]
