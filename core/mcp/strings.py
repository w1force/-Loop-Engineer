"""MCP 工具命名工具。

命名对齐 Claude Code: mcp__<server>__<tool>。
"""
from __future__ import annotations

import re


def normalize_name_for_mcp(name: str) -> str:
    """把 server/tool 名归一化到 API 工具名可接受的字符范围。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def get_mcp_prefix(server_name: str) -> str:
    return f"mcp__{normalize_name_for_mcp(server_name)}__"


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"{get_mcp_prefix(server_name)}{normalize_name_for_mcp(tool_name)}"


def mcp_info_from_string(tool_name: str) -> dict | None:
    parts = tool_name.split("__")
    if len(parts) < 2 or parts[0] != "mcp" or not parts[1]:
        return None
    return {
        "serverName": parts[1],
        "toolName": "__".join(parts[2:]) if len(parts) > 2 else None,
    }
