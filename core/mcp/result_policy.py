"""MCP 工具结果治理策略。

参考 CCB 的 MCP 输出大小治理思想:大结果不能无提示地填满上下文。当前项目
没有 UI,所以在 MCP adapter 返回 executor 前做 inline 限制和完整结果落盘。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .types import MCPToolResult

_TEXT_PAYLOAD_KEYS = {"text", "content", "message", "summary", "output", "result"}


class MCPResultPolicy:
    """把 MCP 文本结果收敛成适合放进模型上下文的形状。"""

    def __init__(
        self,
        max_inline_chars: int = 12_000,
        artifact_dir: str | Path | None = None,
    ):
        self.max_inline_chars = max_inline_chars
        self.artifact_dir = Path(artifact_dir) if artifact_dir else Path(".loop_artifacts") / "mcp"

    def apply(self, server_name: str, tool_name: str, content: str) -> MCPToolResult:
        original_chars = len(content)
        if original_chars <= self.max_inline_chars:
            return MCPToolResult(content=content, original_chars=original_chars)

        artifact_path = self._write_artifact(server_name, tool_name, content)
        inline = content[: self.max_inline_chars].rstrip()
        detail = (
            "\n\n[MCP output truncated: "
            f"{original_chars} chars from {server_name}.{tool_name}; "
            f"showing first {self.max_inline_chars} chars; "
            f"full output: {artifact_path}]"
        )
        return MCPToolResult(
            content=inline + detail,
            truncated=True,
            artifact_path=str(artifact_path),
            original_chars=original_chars,
        )

    def apply_result(
        self,
        server_name: str,
        tool_name: str,
        result: MCPToolResult,
    ) -> MCPToolResult:
        content = result.content
        if result.structured_content is not None:
            content = _format_structured_content(result.structured_content)

        governed = self.apply(server_name, tool_name, content)
        return replace(
            result,
            content=governed.content,
            truncated=governed.truncated,
            artifact_path=governed.artifact_path,
            original_chars=governed.original_chars,
        )

    def _write_artifact(self, server_name: str, tool_name: str, content: str) -> Path:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        safe_server = _safe_name(server_name)
        safe_tool = _safe_name(tool_name)
        path = self.artifact_dir / f"{int(time.time() * 1000)}-{safe_server}.{safe_tool}.txt"
        path.write_text(content, encoding="utf-8")
        return path


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-") or "mcp"


def _try_unwrap_text_payload(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        key, inner = next(iter(value.items()))
        if key in _TEXT_PAYLOAD_KEYS and isinstance(inner, str):
            return inner
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            else:
                return None
        return "\n".join(parts)
    return None


def _format_structured_content(value: Any) -> str:
    unwrapped = _try_unwrap_text_payload(value)
    if unwrapped is not None:
        return unwrapped
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
