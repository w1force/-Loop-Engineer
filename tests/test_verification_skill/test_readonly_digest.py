"""readonly_workspace_digest: identical value to workspace_digest, fingerprint-guarded.

Locks the safety contract of the control-only digest cache: it must never return a
value different from workspace_digest, and any tree change must invalidate it.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from core.verification.runner import (
    readonly_workspace_digest,
    workspace_digest,
)


def test_matches_workspace_digest_and_caches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("hi", encoding="utf-8")

    raw = workspace_digest(tmp_path)
    assert readonly_workspace_digest(tmp_path) == raw
    assert readonly_workspace_digest(tmp_path) == raw  # cache hit, same value


def test_mutation_invalidates_cache(tmp_path: Path) -> None:
    target = tmp_path / "svc.py"
    target.write_text("ok\n", encoding="utf-8")
    first = readonly_workspace_digest(tmp_path)

    target.write_text("changed\n", encoding="utf-8")
    after = readonly_workspace_digest(tmp_path)

    assert after == workspace_digest(tmp_path)  # still the true digest
    assert after != first  # change was detected, not masked by the cache


def test_new_file_invalidates_cache(tmp_path: Path) -> None:
    (tmp_path / "svc.py").write_text("ok\n", encoding="utf-8")
    first = readonly_workspace_digest(tmp_path)
    (tmp_path / "extra.py").write_text("y = 2\n", encoding="utf-8")
    assert readonly_workspace_digest(tmp_path) != first


def test_git_worktree_fast_path(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
        )

    try:
        git("init", "-q")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "svc.py").write_text("ok\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    clean = readonly_workspace_digest(tmp_path)
    assert clean == workspace_digest(tmp_path)

    (tmp_path / "svc.py").write_text("changed\n", encoding="utf-8")  # dirty
    dirty = readonly_workspace_digest(tmp_path)
    assert dirty == workspace_digest(tmp_path)
    assert dirty != clean
