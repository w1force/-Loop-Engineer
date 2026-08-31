from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

from core.builtin_tools.lsp import LSPInput, LSP_TOOL
from core.forked_agent import _fork_can_use_tool
from core.lsp.attachments import inject_lsp_diagnostic_message
from core.lsp.config import LSPServerConfig, default_lsp_server_configs
from core.lsp.diagnostic_registry import (
    Diagnostic,
    DiagnosticFile,
    DiagnosticPosition,
    DiagnosticRange,
    DiagnosticSeverity,
    check_for_lsp_diagnostics,
    clear_delivered_diagnostics_for_file,
    register_pending_lsp_diagnostic,
    reset_all_lsp_diagnostic_state,
)
from core.lsp.manager import create_lsp_server_manager
from core.loop.orchestrator import QueryParams, query_loop
from core.prompts import build_diagnose_system_prompt
from core.registry import get_tools
from core.tools import CanUseDecision, ToolContext
from core.types import AgentState, StreamEvent, ToolUseBlock, UserMessage
from telemetry.tracer import NoopTracer


FAKE_SERVER = r'''
import json
import sys

def read_message():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode().partition(":")
        if name.lower() == "content-length":
            length = int(value.strip())
    return json.loads(sys.stdin.buffer.read(length))

def write(message):
    body = json.dumps(message, separators=(",", ":")).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()

opened_uri = None
while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if method == "initialize":
        write({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {"hoverProvider": True}}})
    elif method == "textDocument/didOpen":
        opened_uri = message["params"]["textDocument"]["uri"]
        write({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": opened_uri,
                "diagnostics": [{
                    "message": "Undefined name",
                    "severity": 1,
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 5}
                    },
                    "source": "fake",
                    "code": "undefined-name"
                }]
            }
        })
    elif method == "textDocument/hover":
        position = message["params"]["position"]
        value = f"{position['line']}:{position['character']}"
        write({"jsonrpc": "2.0", "id": message["id"], "result": {"contents": {"kind": "plaintext", "value": value}}})
    elif method == "workspace/symbol":
        query = message["params"]["query"]
        write({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": [{
                "name": query or "all-symbols",
                "kind": 12,
                "location": {
                    "uri": opened_uri,
                    "range": {
                        "start": {"line": 1, "character": 2},
                        "end": {"line": 1, "character": 4}
                    }
                }
            }]
        })
    elif method == "shutdown":
        write({"jsonrpc": "2.0", "id": message["id"], "result": None})
    elif method == "exit":
        break
'''


@pytest.fixture(autouse=True)
def _reset_diagnostics():
    reset_all_lsp_diagnostic_state()
    yield
    reset_all_lsp_diagnostic_state()


def _config(tmp_path: Path, script: Path) -> LSPServerConfig:
    return LSPServerConfig(
        name="python",
        command=sys.executable,
        args=("-u", str(script)),
        extension_to_language={".py": "python"},
        workspace_folder=str(tmp_path),
        startup_timeout=5,
    )


async def test_lsp_tool_starts_stdio_server_and_converts_position(tmp_path):
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER)
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n")
    manager = create_lsp_server_manager([_config(tmp_path, script)])

    state = AgentState(cwd=str(tmp_path), lsp_manager=manager)
    ctx = ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=state,
    )
    result = await LSP_TOOL.func(
        LSPInput(
            operation="hover",
            file_path=str(source),
            line=1,
            character=1,
        ),
        ctx,
    )

    assert result == "0:0"
    assert manager.is_file_open(str(source))
    await manager.shutdown()


async def test_workspace_symbol_forwards_optional_query(tmp_path):
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER)
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n")
    manager = create_lsp_server_manager([_config(tmp_path, script)])
    ctx = ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=AgentState(cwd=str(tmp_path), lsp_manager=manager),
    )

    result = await LSP_TOOL.func(
        LSPInput(
            operation="workspaceSymbol",
            file_path=str(source),
            line=1,
            character=1,
            query="authenticate",
        ),
        ctx,
    )

    assert isinstance(result, str)
    assert "authenticate" in result
    await manager.shutdown()


async def test_publish_diagnostics_reaches_registry(tmp_path):
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER)
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n")
    manager = create_lsp_server_manager([_config(tmp_path, script)])
    ctx = ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=AgentState(cwd=str(tmp_path), lsp_manager=manager),
    )

    await LSP_TOOL.func(
        LSPInput(
            operation="hover",
            file_path=str(source),
            line=1,
            character=1,
        ),
        ctx,
    )
    diagnostic_sets = check_for_lsp_diagnostics()

    assert len(diagnostic_sets) == 1
    diagnostic = diagnostic_sets[0].files[0].diagnostics[0]
    assert diagnostic.message == "Undefined name"
    assert diagnostic.severity == "Error"
    assert diagnostic.code == "undefined-name"
    await manager.shutdown()


async def test_fork_keeps_lsp_schema_but_denies_at_execution_permission():
    names = [tool.name for tool in get_tools()]
    assert "LSP" in names

    calls: list[str] = []

    async def parent_permission(tool_call):
        calls.append(tool_call.name)
        return CanUseDecision(allow=True)

    fork_permission = _fork_can_use_tool(parent_permission)
    denied = await fork_permission(
        ToolUseBlock(id="lsp", name="LSP", input={})
    )
    allowed = await fork_permission(
        ToolUseBlock(id="read", name="Read", input={})
    )

    assert denied.allow is False
    assert "main agent" in (denied.reason or "")
    assert allowed.allow is True
    assert calls == ["Read"]


def test_lsp_tool_schema_and_supported_languages(tmp_path):
    schema = LSP_TOOL.to_schema()["input_schema"]
    operation = schema["properties"]["operation"]
    assert set(operation["enum"]) == {
        "goToDefinition",
        "findReferences",
        "hover",
        "documentSymbol",
        "workspaceSymbol",
        "goToImplementation",
        "prepareCallHierarchy",
        "incomingCalls",
        "outgoingCalls",
    }
    assert LSPInput(
        operation="hover", file_path="a.py", line=1, character=1
    )
    assert "query" in schema["properties"]
    assert "query" not in schema["required"]

    configs = default_lsp_server_configs(str(tmp_path))
    extensions = {
        extension
        for config in configs
        for extension in config.extension_to_language
    }
    assert extensions == {".py", ".java"}


def _diagnostic(
    index: int, *, severity: DiagnosticSeverity = "Warning"
) -> Diagnostic:
    return Diagnostic(
        message=f"diagnostic-{index}",
        severity=severity,
        range=DiagnosticRange(
            start=DiagnosticPosition(line=index, character=0),
            end=DiagnosticPosition(line=index, character=1),
        ),
    )


def test_diagnostic_registry_deduplicates_and_redelivers_after_edit():
    file = DiagnosticFile(uri="/workspace/a.py", diagnostics=[_diagnostic(1)])
    register_pending_lsp_diagnostic("python", [file])
    assert len(check_for_lsp_diagnostics()) == 1

    register_pending_lsp_diagnostic("python", [file])
    assert check_for_lsp_diagnostics() == []

    clear_delivered_diagnostics_for_file("file:///workspace/a.py")
    register_pending_lsp_diagnostic("python", [file])
    assert len(check_for_lsp_diagnostics()) == 1


def test_diagnostic_registry_limits_per_file_and_total():
    files = [
        DiagnosticFile(
            uri=f"/workspace/{file_index}.py",
            diagnostics=[
                _diagnostic(file_index * 100 + index, severity="Error")
                for index in range(12)
            ],
        )
        for file_index in range(4)
    ]
    register_pending_lsp_diagnostic("python", files)

    result = check_for_lsp_diagnostics()[0]

    assert sum(len(file.diagnostics) for file in result.files) == 30
    assert all(len(file.diagnostics) <= 10 for file in result.files)


def test_diagnostics_are_injected_only_into_main_agent(tmp_path):
    file = DiagnosticFile(uri="/workspace/a.py", diagnostics=[_diagnostic(1)])
    register_pending_lsp_diagnostic("python", [file])
    tools = get_tools()

    fork_state = AgentState(cwd=str(tmp_path), lsp_manager=None)
    assert inject_lsp_diagnostic_message(fork_state, tools) is False
    assert fork_state.messages == []

    manager = create_lsp_server_manager([])
    main_state = AgentState(cwd=str(tmp_path), lsp_manager=manager)
    assert inject_lsp_diagnostic_message(main_state, tools) is True
    message = main_state.messages[-1]
    assert isinstance(message, UserMessage)
    assert "<new-diagnostics>" in message.content
    assert "diagnostic-1" in message.content


class _CaptureProvider:
    def __init__(self):
        self.messages = None

    async def stream(self, **kwargs):
        self.messages = kwargs["messages"]
        yield StreamEvent(
            type="message_start",
            message={"usage": {"input_tokens": 1, "output_tokens": 0}},
        )
        yield StreamEvent(
            type="content_block_start",
            index=0,
            block={"type": "text", "text": ""},
        )
        yield StreamEvent(
            type="content_block_delta",
            index=0,
            delta={"text": "done"},
        )
        yield StreamEvent(type="content_block_stop", index=0)
        yield StreamEvent(
            type="message_delta",
            delta={"stop_reason": "end_turn"},
            message={"usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        yield StreamEvent(type="message_stop")

    def count_tokens(self, messages) -> int:
        return 0


async def test_query_loop_injects_diagnostics_before_next_model_request(tmp_path):
    register_pending_lsp_diagnostic(
        "python",
        [DiagnosticFile(uri="/workspace/a.py", diagnostics=[_diagnostic(7)])],
    )
    manager = create_lsp_server_manager([])
    state = AgentState(
        messages=[UserMessage(content="continue")],
        cwd=str(tmp_path),
        lsp_manager=manager,
    )
    provider = _CaptureProvider()
    params = QueryParams(
        system="",
        model="m",
        max_tokens=32,
        provider=provider,
        abort_signal=asyncio.Event(),
        tools=get_tools(),
        enable_compact=False,
    )

    _ = [item async for item in query_loop(state, params, NoopTracer())]

    assert provider.messages is not None
    assert any(
        isinstance(message, UserMessage)
        and isinstance(message.content, str)
        and "<new-diagnostics>" in message.content
        for message in provider.messages
    )


def test_diagnose_prompt_expresses_search_and_lsp_division():
    prompt = build_diagnose_system_prompt()
    assert "use Glob for file-name patterns and Grep" in prompt
    assert "Use LSP for semantic navigation" in prompt
