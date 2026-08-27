"""Worktree isolation: control(detached@base) vs candidate(fix branch), real git."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from core.verification.workflow import RepairResult, capture_candidate_snapshot
from core.workspaces import WorkspaceManager, WorktreeError

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)

_IGNORE = (".git/**",)


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "loop@test")
    git("config", "user.name", "loop")
    (path / "service.py").write_text("def handle():\n    return timeout()\n", "utf-8")
    (path / "keep.py").write_text("UNCHANGED = 1\n", "utf-8")
    git("add", "-A")
    git("commit", "-m", "base")
    return git("rev-parse", "HEAD")


def test_prepare_creates_isolated_control_and_candidate(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    manager = WorkspaceManager(root=tmp_path / "wt")

    pair = manager.prepare(
        repo=repo, control_ref="main", incident_id="inc-1", run_id="run-1"
    )

    control = Path(pair.control_workspace)
    candidate = Path(pair.candidate_workspace)
    assert control.is_dir() and candidate.is_dir()
    assert control != candidate
    assert pair.control_sha == base
    assert pair.candidate_branch == "fix/inc-1"
    # both start identical to the base commit
    assert (control / "service.py").read_text("utf-8") == (
        candidate / "service.py"
    ).read_text("utf-8")

    # repair edits ONLY the candidate worktree
    (candidate / "service.py").write_text(
        "def handle():\n    try:\n        return timeout()\n"
        "    except TimeoutError:\n        return fallback()\n",
        "utf-8",
    )

    # control is untouched by the candidate edit (isolation)
    assert "except TimeoutError" not in (control / "service.py").read_text("utf-8")

    snapshot = capture_candidate_snapshot(
        control_workspace=str(control),
        repair=RepairResult(
            workspace=str(candidate),
            candidate_ref="candidate:run-1:1",
            implementation_summary="add fallback",
            test_entrypoints=("pytest",),
        ),
        workspace_ignore=_IGNORE,
    )
    # the byte-level diff sees ONLY the repair edit; .git is ignored, keep.py stable
    changed = {c.path for c in snapshot.changed_files}
    assert changed == {"service.py"}
    assert "except TimeoutError" in snapshot.unified_diff

    manager.cleanup(pair)


def test_control_is_pinned_to_frozen_commit_even_if_base_moves(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    manager = WorkspaceManager(root=tmp_path / "wt")
    pair = manager.prepare(
        repo=repo, control_ref=base, incident_id="inc-2", run_id="run-2"
    )

    # main advances after the incident was frozen
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "later work"],
        check=True,
        capture_output=True,
    )
    # control worktree still reflects the frozen base, not the new tip
    head = subprocess.run(
        ["git", "-C", pair.control_workspace, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == base

    manager.cleanup(pair)


def test_cleanup_keeps_fix_branch_for_release(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    manager = WorkspaceManager(root=tmp_path / "wt")
    pair = manager.prepare(
        repo=repo, control_ref="main", incident_id="inc-3", run_id="run-3"
    )
    manager.cleanup(pair)  # default keeps the branch

    assert not Path(pair.control_workspace).exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "fix/inc-3"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "fix/inc-3" in branches


def test_prepare_rejects_missing_ref(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    manager = WorkspaceManager(root=tmp_path / "wt")
    with pytest.raises(WorktreeError):
        manager.prepare(
            repo=repo, control_ref="does-not-exist", incident_id="x", run_id="run-4"
        )
