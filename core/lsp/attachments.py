"""把注册表中的被动诊断作为下一轮主 Agent system-reminder 注入。"""
from __future__ import annotations

from pathlib import Path

from ..tools import Tool
from ..types import AgentState, UserMessage
from .diagnostic_registry import (
    DiagnosticFile,
    check_for_lsp_diagnostics,
    clear_all_lsp_diagnostics,
)

MAX_DIAGNOSTICS_SUMMARY_CHARS = 4000


def format_diagnostics_summary(files: list[DiagnosticFile]) -> str:
    symbols = {"Error": "✘", "Warning": "⚠", "Info": "ℹ", "Hint": "★"}
    sections: list[str] = []
    for file in files:
        lines = []
        for diagnostic in file.diagnostics:
            location = diagnostic.range.start
            suffix = f" [{diagnostic.code}]" if diagnostic.code else ""
            suffix += f" ({diagnostic.source})" if diagnostic.source else ""
            lines.append(
                f"  {symbols[diagnostic.severity]} "
                f"[Line {location.line + 1}:{location.character + 1}] "
                f"{diagnostic.message}{suffix}"
            )
        sections.append(f"{Path(file.uri).name or file.uri}:\n" + "\n".join(lines))
    result = "\n\n".join(sections)
    marker = "…[truncated]"
    if len(result) > MAX_DIAGNOSTICS_SUMMARY_CHARS:
        return result[: MAX_DIAGNOSTICS_SUMMARY_CHARS - len(marker)] + marker
    return result


def inject_lsp_diagnostic_message(
    agent_state: AgentState, tools: list[Tool]
) -> bool:
    """仅主 Agent 在请求模型前取走诊断；返回本轮是否注入了消息。"""
    if agent_state.lsp_manager is None:
        return False
    if not any(tool.name == "Bash" for tool in tools):
        return False

    diagnostic_sets = check_for_lsp_diagnostics()
    if not diagnostic_sets:
        return False
    files = [file for diagnostic_set in diagnostic_sets for file in diagnostic_set.files]
    summary = format_diagnostics_summary(files)
    agent_state.messages.append(
        UserMessage(
            content=(
                "<system-reminder>\n"
                "<new-diagnostics>The following new diagnostic issues were detected:\n\n"
                f"{summary}</new-diagnostics>\n"
                "</system-reminder>"
            )
        )
    )
    clear_all_lsp_diagnostics()
    return True
