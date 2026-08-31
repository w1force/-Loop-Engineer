"""Fail-closed path guard for fresh-context workflow Agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
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
    return _resolve_workspace_path(value, workspace) is not None


def _resolve_workspace_path(value: str, workspace: Path) -> Path | None:
    try:
        expanded = Path(value).expanduser()
        resolved = (
            expanded.resolve()
            if expanded.is_absolute()
            else (workspace / expanded).resolve()
        )
        resolved.relative_to(workspace)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_restricted_paths(
    values: Iterable[str | Path], workspace: Path
) -> tuple[Path, ...]:
    resolved_paths: list[Path] = []
    for value in values:
        relative = Path(value)
        if (
            relative.is_absolute()
            or relative == Path(".")
            or ".." in relative.parts
        ):
            raise ValueError(
                "restricted workspace paths must be non-empty relative paths"
            )
        resolved = (workspace / relative).resolve()
        if not _is_within(resolved, workspace):
            raise ValueError("restricted workspace path escapes the workspace")
        if resolved not in resolved_paths:
            resolved_paths.append(resolved)
    return tuple(resolved_paths)


def restricted_paths_for_workspace(
    workspace: str | Path,
    protected_roots: Iterable[str | Path],
) -> tuple[Path, ...]:
    """Require trusted control-plane roots to be disjoint from an Agent workspace.

    A lexical path filter cannot stop an arbitrary local test or source file from
    opening a sibling subtree. Any overlap therefore fails closed; disjoint roots
    are already unreachable through the workspace-scoped tools.
    """

    root = Path(workspace).expanduser().resolve()
    restricted: list[Path] = []
    for value in protected_roots:
        candidate = Path(value).expanduser()
        protected = (
            candidate.resolve()
            if candidate.is_absolute()
            else (Path.cwd() / candidate).resolve()
        )
        if _is_within(root, protected) or _is_within(protected, root):
            raise ValueError(
                "workflow workspace must not overlap a protected control-plane root"
            )
    return tuple(restricted)


def _touches_restricted_path(
    value: str,
    workspace: Path,
    restricted_paths: tuple[Path, ...],
    *,
    searches_descendants: bool,
) -> bool:
    resolved = _resolve_workspace_path(value, workspace)
    if resolved is None:
        return False
    return any(
        _is_within(resolved, restricted)
        or (searches_descendants and _is_within(restricted, resolved))
        for restricted in restricted_paths
    )


def _bash_paths_stay_in_workspace(
    command: str,
    workspace: Path,
    restricted_paths: tuple[Path, ...] = (),
) -> bool:
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
    command_tokens = list(tokens)
    if command_tokens and Path(command_tokens[0]).name == "env":
        command_tokens.pop(0)
    while (
        command_tokens
        and "=" in command_tokens[0]
        and command_tokens[0].partition("=")[0].replace("_", "a").isalnum()
    ):
        command_tokens.pop(0)
    executable = Path(command_tokens[0]).name if command_tokens else ""
    explicit_path_seen = False
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
        explicit_path_seen = True
        if not _within_workspace(candidate, workspace):
            return False
        if _touches_restricted_path(
            candidate,
            workspace,
            restricted_paths,
            searches_descendants=True,
        ):
            return False
    if restricted_paths and executable in {"rg", "ripgrep", "fd", "fdfind", "find"}:
        # These commands recursively search cwd when no explicit root is supplied;
        # that implicit traversal would bypass the restricted-subtree checks above.
        if not explicit_path_seen:
            return False
    if restricted_paths and executable in {"grep", "egrep", "fgrep"}:
        recursive = any(
            token in {"-r", "-R", "--recursive", "--dereference-recursive"}
            or (
                token.startswith("-")
                and not token.startswith("--")
                and any(flag in token[1:] for flag in ("r", "R"))
            )
            for token in command_tokens[1:]
        )
        if recursive and not explicit_path_seen:
            return False
    if restricted_paths and executable == "git" and len(command_tokens) > 1:
        if command_tokens[1] == "grep":
            return False
        for token in command_tokens[2:]:
            _, separator, git_path = token.partition(":")
            if separator and git_path and _touches_restricted_path(
                git_path,
                workspace,
                restricted_paths,
                searches_descendants=True,
            ):
                return False
    return True


def build_workspace_guard(
    parent_can_use_tool: Callable[[ToolUseBlock], Awaitable[CanUseDecision]],
    *,
    workspace: str | Path,
    allowed_tool_names: frozenset[str],
    restricted_relative_paths: Iterable[str | Path] = (),
) -> Callable[[ToolUseBlock], Awaitable[CanUseDecision]]:
    """Constrain tools to a workspace and optional inaccessible subtrees."""

    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Agent workspace does not exist")
    restricted_paths = _resolve_restricted_paths(restricted_relative_paths, root)

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
            search_path = (
                raw
                if isinstance(raw, str)
                else "."
                if tool_call.name in {"Glob", "Grep"}
                else None
            )
            if search_path is not None and _touches_restricted_path(
                search_path,
                root,
                restricted_paths,
                searches_descendants=tool_call.name in {"Glob", "Grep"},
            ):
                return CanUseDecision(
                    allow=False,
                    reason=(
                        f"{tool_call.name} path intersects a restricted workflow "
                        "workspace subtree"
                    ),
                )
        if tool_call.name == "Bash":
            command = tool_call.input.get("command")
            if not isinstance(command, str) or not _bash_paths_stay_in_workspace(
                command, root, restricted_paths
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


__all__ = ["build_workspace_guard", "restricted_paths_for_workspace"]
