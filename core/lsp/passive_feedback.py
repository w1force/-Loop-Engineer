"""接收 ``textDocument/publishDiagnostics`` 并写入异步诊断注册表。"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging

from .diagnostic_registry import (
    Diagnostic,
    DiagnosticFile,
    DiagnosticPosition,
    DiagnosticRange,
    DiagnosticSeverity,
    normalize_diagnostic_uri,
    register_pending_lsp_diagnostic,
)
from .manager import LSPServerManager

logger = logging.getLogger("lsp.diagnostics")


def _severity(value: object) -> DiagnosticSeverity:
    if value == 2:
        return "Warning"
    if value == 3:
        return "Info"
    if value == 4:
        return "Hint"
    return "Error"


def _position(value: object) -> DiagnosticPosition:
    if not isinstance(value, dict):
        return DiagnosticPosition(line=0, character=0)
    return DiagnosticPosition(
        line=int(value.get("line", 0)),
        character=int(value.get("character", 0)),
    )


def format_diagnostics_for_attachment(params: object) -> list[DiagnosticFile]:
    if not isinstance(params, dict):
        raise ValueError("publishDiagnostics params must be an object")
    uri = params.get("uri")
    raw_diagnostics = params.get("diagnostics")
    if not isinstance(uri, str) or not isinstance(raw_diagnostics, list):
        raise ValueError("publishDiagnostics requires uri and diagnostics")

    diagnostics: list[Diagnostic] = []
    for raw in raw_diagnostics:
        if not isinstance(raw, dict):
            continue
        raw_range = raw.get("range")
        raw_range = raw_range if isinstance(raw_range, dict) else {}
        code = raw.get("code")
        source = raw.get("source")
        diagnostics.append(
            Diagnostic(
                message=str(raw.get("message", "")),
                severity=_severity(raw.get("severity")),
                range=DiagnosticRange(
                    start=_position(raw_range.get("start")),
                    end=_position(raw_range.get("end")),
                ),
                source=str(source) if source is not None else None,
                code=str(code) if code is not None else None,
            )
        )
    return [
        DiagnosticFile(
            uri=normalize_diagnostic_uri(uri),
            diagnostics=diagnostics,
        )
    ]


@dataclass
class HandlerRegistrationResult:
    total_servers: int
    success_count: int = 0
    registration_errors: list[tuple[str, str]] = field(default_factory=list)
    diagnostic_failures: dict[str, tuple[int, str]] = field(default_factory=dict)


def register_lsp_notification_handlers(
    manager: LSPServerManager,
) -> HandlerRegistrationResult:
    """为所有已配置 server 注册被动诊断处理器；单 server 失败不影响其他 server。"""
    servers = manager.get_all_servers()
    result = HandlerRegistrationResult(total_servers=len(servers))

    for server_name, server in servers.items():
        try:
            def handle(params: object, *, name: str = server_name) -> None:
                try:
                    files = format_diagnostics_for_attachment(params)
                    if not files or not files[0].diagnostics:
                        return
                    register_pending_lsp_diagnostic(name, files)
                    result.diagnostic_failures.pop(name, None)
                except Exception as error:
                    count, _ = result.diagnostic_failures.get(name, (0, ""))
                    result.diagnostic_failures[name] = (count + 1, str(error))
                    logger.warning(
                        "Failed to process LSP diagnostics from %s", name, exc_info=True
                    )

            server.on_notification("textDocument/publishDiagnostics", handle)
            result.success_count += 1
        except Exception as error:
            result.registration_errors.append((server_name, str(error)))
            logger.warning(
                "Failed to register diagnostics handler for %s",
                server_name,
                exc_info=True,
            )
    return result
