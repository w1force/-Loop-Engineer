"""LSP 工具：把模型工具调用翻译成标准 LSP request。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, Field

from ..file_state import expand_path
from ..lsp.constants import LSP_TOOL_NAME, MAX_LSP_FILE_SIZE_BYTES
from ..tools import ToolContext, build_tool

LSPOperation = Literal[
    "goToDefinition",
    "findReferences",
    "hover",
    "documentSymbol",
    "workspaceSymbol",
    "goToImplementation",
    "prepareCallHierarchy",
    "incomingCalls",
    "outgoingCalls",
]


class LSPInput(BaseModel):
    operation: LSPOperation = Field(description="要执行的 LSP 操作")
    file_path: str = Field(description="要分析的文件路径")
    line: int = Field(ge=1, description="编辑器中显示的行号(从 1 开始)")
    character: int = Field(ge=1, description="编辑器中显示的字符偏移(从 1 开始)")
    query: str | None = Field(
        default=None,
        description=(
            "仅 workspaceSymbol 使用的可选符号查询词；省略时请求工作区全部符号"
        ),
    )


def _method_and_params(
    inp: LSPInput, file_path: str
) -> tuple[str, dict[str, object]]:
    uri = Path(file_path).resolve().as_uri()
    position = {"line": inp.line - 1, "character": inp.character - 1}
    text_document = {"uri": uri}
    if inp.operation == "goToDefinition":
        return "textDocument/definition", {
            "textDocument": text_document,
            "position": position,
        }
    if inp.operation == "findReferences":
        return "textDocument/references", {
            "textDocument": text_document,
            "position": position,
            "context": {"includeDeclaration": True},
        }
    if inp.operation == "hover":
        return "textDocument/hover", {
            "textDocument": text_document,
            "position": position,
        }
    if inp.operation == "documentSymbol":
        return "textDocument/documentSymbol", {"textDocument": text_document}
    if inp.operation == "workspaceSymbol":
        return "workspace/symbol", {"query": inp.query or ""}
    if inp.operation == "goToImplementation":
        return "textDocument/implementation", {
            "textDocument": text_document,
            "position": position,
        }
    return "textDocument/prepareCallHierarchy", {
        "textDocument": text_document,
        "position": position,
    }


def _display_path(uri: object, cwd: str) -> str:
    if not isinstance(uri, str):
        return "<unknown>"
    parsed = urlparse(uri)
    path = unquote(parsed.path) if parsed.scheme == "file" else uri
    try:
        return str(Path(path).relative_to(Path(cwd).resolve()))
    except ValueError:
        return path


def _position(value: object) -> str:
    if not isinstance(value, dict):
        return "?:?"
    return f"{int(value.get('line', 0)) + 1}:{int(value.get('character', 0)) + 1}"


def _location_lines(result: object, cwd: str) -> list[str]:
    values = result if isinstance(result, list) else [result]
    lines: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        uri = value.get("uri") or value.get("targetUri")
        range_value = value.get("range") or value.get("targetSelectionRange")
        start = range_value.get("start") if isinstance(range_value, dict) else None
        lines.append(f"{_display_path(uri, cwd)}:{_position(start)}")
    return lines


def _hover_text(result: object) -> str:
    if not isinstance(result, dict):
        return "No hover information available."
    contents = result.get("contents")
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return str(contents.get("value", contents))
    if isinstance(contents, list):
        parts = [
            str(item.get("value", item)) if isinstance(item, dict) else str(item)
            for item in contents
        ]
        return "\n\n".join(parts)
    return "No hover information available."


def _symbol_lines(
    values: object, cwd: str, *, depth: int = 0
) -> list[str]:
    if not isinstance(values, list):
        return []
    lines: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        name = str(value.get("name", "<anonymous>"))
        location = value.get("location")
        if isinstance(location, dict):
            suffix = (
                f" — {_display_path(location.get('uri'), cwd)}:"
                f"{_position((location.get('range') or {}).get('start'))}"
            )
        else:
            suffix = f" — {_position((value.get('selectionRange') or value.get('range') or {}).get('start'))}"
        lines.append(f"{'  ' * depth}{name}{suffix}")
        lines.extend(_symbol_lines(value.get("children"), cwd, depth=depth + 1))
    return lines


def _call_lines(result: object, cwd: str, direction: str) -> list[str]:
    if not isinstance(result, list):
        return []
    lines: list[str] = []
    for value in result:
        if not isinstance(value, dict):
            continue
        item = value.get(direction, value)
        if not isinstance(item, dict):
            continue
        lines.append(
            f"{item.get('name', '<anonymous>')} — "
            f"{_display_path(item.get('uri'), cwd)}:"
            f"{_position((item.get('selectionRange') or item.get('range') or {}).get('start'))}"
        )
    return lines


def _format_result(operation: LSPOperation, result: object, cwd: str) -> str:
    if operation in (
        "goToDefinition",
        "goToImplementation",
        "findReferences",
    ):
        lines = _location_lines(result, cwd)
    elif operation == "hover":
        return _hover_text(result)
    elif operation in ("documentSymbol", "workspaceSymbol"):
        lines = _symbol_lines(result, cwd)
    elif operation == "incomingCalls":
        lines = _call_lines(result, cwd, "from")
    elif operation == "outgoingCalls":
        lines = _call_lines(result, cwd, "to")
    else:
        lines = _call_lines(result, cwd, "")
    if lines:
        return "\n".join(lines)
    if result is None or result == []:
        return f"No results for {operation}."
    return json.dumps(result, ensure_ascii=False, indent=2)


async def _lsp_func(inp: LSPInput, ctx: ToolContext) -> str:
    manager = ctx.agent_state.lsp_manager
    if manager is None:
        return "LSP server manager not initialized."
    file_path = expand_path(inp.file_path)
    path = Path(file_path)
    if not path.exists():
        raise ValueError(f"文件不存在: {inp.file_path}")
    if not path.is_file():
        raise ValueError(f"路径不是文件: {inp.file_path}")
    if path.suffix.lower() not in (".java", ".py"):
        return f"No LSP server available for file type: {path.suffix}"
    if path.stat().st_size > MAX_LSP_FILE_SIZE_BYTES:
        return "File too large for LSP analysis (10MB limit)."

    if not manager.is_file_open(file_path):
        await manager.open_file(file_path, path.read_text(encoding="utf-8"))

    method, params = _method_and_params(inp, file_path)
    result = await manager.send_request(file_path, method, params)
    if inp.operation in ("incomingCalls", "outgoingCalls"):
        if not isinstance(result, list) or not result:
            return "No call hierarchy item found at this position."
        call_method = (
            "callHierarchy/incomingCalls"
            if inp.operation == "incomingCalls"
            else "callHierarchy/outgoingCalls"
        )
        result = await manager.send_request(
            file_path, call_method, {"item": result[0]}
        )
    return _format_result(inp.operation, result, ctx.agent_state.cwd)


LSP_TOOL = build_tool(
    name=LSP_TOOL_NAME,
    description=(
        "通过 Language Server Protocol 获取 Java/Python 的语义代码智能。适合在已知文件、"
        "符号或位置后追踪定义、引用、接口实现、类型信息和调用关系。支持 goToDefinition、"
        "findReferences、hover、documentSymbol、workspaceSymbol、goToImplementation、"
        "prepareCallHierarchy、incomingCalls、outgoingCalls。workspaceSymbol 可用 query "
        "搜索符号；line/character 均从 1 开始。"
    ),
    input_model=LSPInput,
    func=_lsp_func,
    is_concurrency_safe=True,
)
