"""Workspace provisioning: control/candidate git-worktree isolation."""

from __future__ import annotations

from .manager import (
    DEFAULT_WORKTREE_IGNORE,
    WorkspaceManager,
    WorkspacePair,
    WorktreeError,
)

__all__ = [
    "DEFAULT_WORKTREE_IGNORE",
    "WorkspaceManager",
    "WorkspacePair",
    "WorktreeError",
]
