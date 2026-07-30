"""MCP 配置文件加载。

配置形状对齐 Claude Code 的 .mcp.json:顶层 mcpServers,每个 server 声明
type/command/args/env。当前真实 client 只支持 stdio,其他 transport 先明确拒绝。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .types import MCPServerConfig, MCPTransport


def load_mcp_configs(config_items: list[str]) -> list[MCPServerConfig]:
    """读取多个 MCP 配置项并合并。

    对齐 CCB 的 --mcp-config 语义:每个 item 可以是 JSON 字符串或文件路径;
    后面的配置覆盖前面同名 server。
    """

    merged: dict[str, dict[str, Any]] = {}
    for item in config_items:
        item = item.strip()
        if not item:
            continue
        parsed = _parse_config_item(item)
        merged.update(parsed)
    return _configs_from_servers(merged)


def load_mcp_configs_from_file(path: str | Path) -> list[MCPServerConfig]:
    """从 JSON 文件读取 MCP server 配置。"""

    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"MCP config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid MCP config JSON: {config_path}: {exc}") from exc

    return _configs_from_servers(_extract_mcp_servers(raw))


def _parse_config_item(item: str) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(item)
    except json.JSONDecodeError:
        config_path = Path(item)
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"MCP config file not found: {config_path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid MCP config JSON: {config_path}: {exc}") from exc
    return _extract_mcp_servers(raw)


def _extract_mcp_servers(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("mcpServers"), dict):
        raise ValueError("MCP config must contain object field 'mcpServers'")
    servers: dict[str, dict[str, Any]] = {}
    for name, server in raw["mcpServers"].items():
        if not isinstance(name, str):
            raise ValueError("mcpServers keys must be strings")
        if not isinstance(server, dict):
            raise ValueError(f"mcpServers.{name} must be an object")
        servers[name] = server
    return servers


def _configs_from_servers(servers: dict[str, dict[str, Any]]) -> list[MCPServerConfig]:
    configs: list[MCPServerConfig] = []
    for name in sorted(servers):
        server = servers[name]
        configs.append(_server_config_from_dict(name, server))
    return configs


def _server_config_from_dict(name: str, raw: dict[str, Any]) -> MCPServerConfig:
    expanded = _expand_config_env(raw, path=f"mcpServers.{name}")
    transport_value = str(expanded.get("type") or "stdio")
    try:
        transport = MCPTransport(transport_value)
    except ValueError as exc:
        raise ValueError(f"Unsupported MCP transport for {name}: {transport_value}") from exc

    if transport != MCPTransport.STDIO:
        raise ValueError(
            f"Unsupported MCP transport for {name}: {transport.value}. "
            "Only stdio has a real client now."
        )

    command = expanded.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError(f"mcpServers.{name}.command must be a non-empty string")

    args = expanded.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValueError(f"mcpServers.{name}.args must be a string list")

    env = expanded.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise ValueError(f"mcpServers.{name}.env must be a string map")

    timeout = expanded.get("timeout", 10.0)
    if not isinstance(timeout, int | float):
        raise ValueError(f"mcpServers.{name}.timeout must be a number")

    disabled = expanded.get("disabled", False)
    if not isinstance(disabled, bool):
        raise ValueError(f"mcpServers.{name}.disabled must be a boolean")

    return MCPServerConfig(
        name=name,
        command=command,
        args=list(args),
        env=dict(env),
        timeout=float(timeout),
        transport=transport,
        disabled=disabled,
    )


def _expand_config_env(value: Any, *, path: str) -> Any:
    if isinstance(value, str):
        return _expand_env_string(value, path=path)
    if isinstance(value, list):
        return [_expand_config_env(item, path=path) for item in value]
    if isinstance(value, dict):
        return {key: _expand_config_env(item, path=path) for key, item in value.items()}
    return value


def _expand_env_string(value: str, *, path: str) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        var_name, default = _split_env_default(expr)
        if var_name in os.environ:
            return os.environ[var_name]
        if default is not None:
            return default
        missing.append(var_name)
        return match.group(0)

    expanded = re.sub(r"\$\{([^}]+)\}", replace, value)
    if missing:
        raise ValueError(
            f"Missing environment variables in {path}: {', '.join(sorted(set(missing)))}"
        )
    return expanded


def _split_env_default(expr: str) -> tuple[str, str | None]:
    if ":-" in expr:
        name, default = expr.split(":-", 1)
        return name, default
    return expr, None
