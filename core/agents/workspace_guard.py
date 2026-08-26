"""Fail-closed path guard for fresh-context workflow Agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
import shlex

from core.tools import CanUseDecision
from core.types import ToolUseBlock


_PATH_FIELDS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "Glob": "path",
    "Grep": "path",
}


def _has_dynamic_shell_path_expansion(command: str) -> bool:
    """Return whether Bash could derive a path that was not checked literally.

    ``shlex.split`` does not expand parameters, substitutions, globs, braces, or
    tildes, while the Bash tool later executes the original string with
    ``bash -c``.  Reject those forms outside single quotes so a token such as
    ``$HOME/tests`` cannot look workspace-relative here and escape at runtime.
    This remains a lexical guard, not an OS sandbox.
    """

    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote == "'":
            if character == "'":
                quote = None
            index += 1
            continue
        if character == "\\":
            escaped = True
            index += 1
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            index += 1
            continue
        if character in {"$", "`"}:
            return True
        if quote is None:
            if character in {"~", "*", "?", "[", "{", "}"}:
                return True
            if character in {"<", ">"} and index + 1 < len(command):
                if command[index + 1] == "(":
                    return True
        index += 1
    return escaped or quote is not None


def _within_workspace(value: str, workspace: Path) -> bool:
    try:
        expanded = Path(value).expanduser()
        resolved = (
            expanded.resolve()
            if expanded.is_absolute()
            else (workspace / expanded).resolve()
        )
        resolved.relative_to(workspace)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _bash_paths_stay_in_workspace(command: str, workspace: Path) -> bool:
    """Reject explicit path escapes before the narrower Bash policy runs.

    This is intentionally conservative. It is not a shell sandbox; complex shell
    syntax is rejected separately by the verification command policy.
    """

    if _has_dynamic_shell_path_expansion(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    for index, token in enumerate(tokens):
        candidate = token.partition("=")[2] if token.startswith("--") and "=" in token else token
        if not candidate or candidate.startswith("-"):
            continue
        # Pytest node ids keep the filesystem path before ``::``.  Resolve every
        # explicit path, including an existing plain relative name such as a
        # symlink called ``leak``; checking only absolute/``..`` paths lets that
        # symlink escape the Coordinator-selected workspace.
        candidate = candidate.split("::", 1)[0]
        path = Path(candidate).expanduser()
        relative = workspace / path if not path.is_absolute() else path
        looks_like_path = (
            path.is_absolute()
            or ".." in path.parts
            or candidate.startswith(".")
            or "/" in candidate
            or "\\" in candidate
            or relative.exists()
            or relative.is_symlink()
        )
        if not looks_like_path:
            continue
        if index == 0 and not path.is_absolute() and "/" not in candidate and "\\" not in candidate:
            # A bare executable name is resolved by PATH, not against the
            # workspace, so it is not a caller-supplied filesystem path.
            continue
        if not _within_workspace(candidate, workspace):
            return False
    return True


def build_workspace_guard(
    parent_can_use_tool: Callable[[ToolUseBlock], Awaitable[CanUseDecision]],
    *,
    workspace: str | Path,
    allowed_tool_names: frozenset[str],
) -> Callable[[ToolUseBlock], Awaitable[CanUseDecision]]:
    """Constrain path-bearing tools to one Coordinator-selected workspace."""

    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Agent workspace does not exist")

    async def can_use_tool(tool_call: ToolUseBlock) -> CanUseDecision:
        if tool_call.name not in allowed_tool_names:
            return CanUseDecision(
                allow=False,
                reason=f"workflow Agent cannot use {tool_call.name}",
            )
        field = _PATH_FIELDS.get(tool_call.name)
        if field is not None:
            raw = tool_call.input.get(field)
            if raw is not None and (
                not isinstance(raw, str) or not _within_workspace(raw, root)
            ):
                return CanUseDecision(
                    allow=False,
                    reason=(
                        f"{tool_call.name} path must remain inside the frozen "
                        "workflow workspace"
                    ),
                )
        if tool_call.name == "Bash":
            command = tool_call.input.get("command")
            if not isinstance(command, str) or not _bash_paths_stay_in_workspace(
                command, root
            ):
                return CanUseDecision(
                    allow=False,
                    reason=(
                        "Bash command contains an outside or dynamically expanded "
                        "workflow path"
                    ),
                )
        return await parent_can_use_tool(tool_call)

    return can_use_tool


__all__ = ["build_workspace_guard"]
