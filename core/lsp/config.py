"""Java/Python LSP server 配置。

Claude Code 从插件的 ``.lsp.json`` 加载 command/args/extensionToLanguage。
Loop Engineer 当前只支持 Java 与 Python，因此保留同一配置形状，但使用两个固定语言
配置；命令和参数可通过 LOOP_ENGINEER_* 环境变量覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import shlex


@dataclass(frozen=True)
class LSPServerConfig:
    name: str
    command: str
    args: tuple[str, ...]
    extension_to_language: dict[str, str]
    workspace_folder: str
    env: dict[str, str] = field(default_factory=dict)
    initialization_options: object = field(default_factory=dict)
    startup_timeout: float = 30.0


def _args_from_env(name: str, default: str) -> tuple[str, ...]:
    return tuple(shlex.split(os.getenv(name, default)))


def default_lsp_server_configs(cwd: str) -> list[LSPServerConfig]:
    """返回唯一受支持的两个语言服务器配置。"""
    timeout = float(os.getenv("LOOP_ENGINEER_LSP_STARTUP_TIMEOUT", "30"))
    return [
        LSPServerConfig(
            name="python",
            command=os.getenv(
                "LOOP_ENGINEER_PYTHON_LSP_COMMAND", "pyright-langserver"
            ),
            args=_args_from_env(
                "LOOP_ENGINEER_PYTHON_LSP_ARGS", "--stdio"
            ),
            extension_to_language={".py": "python"},
            workspace_folder=cwd,
            startup_timeout=timeout,
        ),
        LSPServerConfig(
            name="java",
            command=os.getenv("LOOP_ENGINEER_JAVA_LSP_COMMAND", "jdtls"),
            args=_args_from_env("LOOP_ENGINEER_JAVA_LSP_ARGS", ""),
            extension_to_language={".java": "java"},
            workspace_folder=cwd,
            startup_timeout=timeout,
        ),
    ]
