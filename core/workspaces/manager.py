"""Git-worktree provisioning for control/candidate isolation.

Materializes the control/candidate pair as two ``git worktree`` directories that
share one ``.git`` but sit on different refs:

    control   -> detached HEAD at the frozen base commit (incident.control_ref)  [read-only baseline]
    candidate -> branch ``fix/<incident_id>`` reset to that same base commit      [repair edits here]

This is the git-native form of "prod = base branch, pre-prod = fix branch": both
are checked out simultaneously so Docker can build an A/B pair, while the diff is a
plain ``git diff`` and the candidate branch is exactly what Release later pushes as
a PR. The verification core is unchanged — it already consumes two workspace paths,
and the default ``VerificationPolicy.workspace_ignore`` (``.git/**``) already hides
each worktree's ``.git`` file so the byte-level snapshot sees only the repair edits.

Typical bootstrap wiring (provisioning sits ABOVE the orchestrator):

    manager = WorkspaceManager(root=".loop-engineer/worktrees")
    pair = manager.prepare(repo=repo, control_ref=incident.control_ref,
                           incident_id=incident.incident_id, run_id=run_id)
    diag = DiagnosisRequest(..., control_workspace=pair.control_workspace)
    req = LoopRunRequest(diagnosis=diag, candidate_workspace=pair.candidate_workspace)
    outcome = await loop_engineer.run(req, ...)
    manager.cleanup(pair)          # keeps the fix branch for the PR by default

The repair agent never commits (PRD red line): it only edits files in the candidate
worktree; the trusted CandidateSnapshotter computes the diff/digest, and Release is
the only component that commits + pushes the branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

# Aligns with VerificationPolicy.workspace_ignore defaults; ".git/**" also hides the
# linked worktree's top-level ".git" file (see runner._ignored prefix handling).
DEFAULT_WORKTREE_IGNORE: tuple[str, ...] = (
    ".git/**",
    ".loop-engineer/**",
    "__pycache__/**",
    "*.pyc",
)


class WorktreeError(RuntimeError):
    """Worktree provisioning failed closed."""


@dataclass(frozen=True)
class WorkspacePair:
    repo: str
    control_ref: str          # the input ref (e.g. incident.control_ref)
    control_sha: str          # the pinned commit the control worktree is detached at
    control_workspace: str
    candidate_workspace: str
    candidate_branch: str
    workspace_ignore: tuple[str, ...]


class WorkspaceManager:
    """Provisions and tears down control/candidate git worktrees per run."""

    def __init__(
        self,
        root: str | Path,
        *,
        git_bin: str = "git",
        extra_ignore: tuple[str, ...] = (),
    ):
        self.root = Path(root).expanduser().resolve()
        self.git_bin = git_bin
        self.workspace_ignore = tuple(
            dict.fromkeys((*DEFAULT_WORKTREE_IGNORE, *extra_ignore))
        )

    # ── git helpers ───────────────────────────────────────────────────────────
    def _git(self, repo: str | Path, *args: str) -> str:
        proc = subprocess.run(
            [self.git_bin, "-C", str(repo), *args],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise WorktreeError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    def _resolve_commit(self, repo: Path, ref: str) -> str:
        try:
            return self._git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        except WorktreeError as exc:
            raise WorktreeError(f"control ref does not resolve to a commit: {ref}") from exc

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def prepare(
        self,
        *,
        repo: str | Path,
        control_ref: str,
        incident_id: str,
        run_id: str,
        candidate_branch: str | None = None,
    ) -> WorkspacePair:
        """Create the control (detached@base) and candidate (fix branch) worktrees."""

        repo = Path(repo).expanduser().resolve()
        if not (repo / ".git").exists():
            raise WorktreeError(f"not a git repository: {repo}")
        base_sha = self._resolve_commit(repo, control_ref)

        run_dir = self.root / _safe_segment(run_id)
        control_dir = run_dir / "control"
        candidate_dir = run_dir / "candidate"
        if run_dir.exists():
            raise WorktreeError(f"workspace for run already exists: {run_dir}")
        run_dir.mkdir(parents=True)

        branch = candidate_branch or f"fix/{incident_id}"

        # Control: detached at the exact frozen commit — a read-only baseline that
        # does NOT move with the base branch.
        self._git(repo, "worktree", "add", "--detach", str(control_dir), base_sha)
        try:
            # Candidate: (re)create the fix branch pinned to the same base commit.
            self._git(
                repo, "worktree", "add", "-B", branch, str(candidate_dir), base_sha
            )
        except WorktreeError:
            self._safe_remove(repo, control_dir)
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

        return WorkspacePair(
            repo=str(repo),
            control_ref=control_ref,
            control_sha=base_sha,
            control_workspace=str(control_dir),
            candidate_workspace=str(candidate_dir),
            candidate_branch=branch,
            workspace_ignore=self.workspace_ignore,
        )

    def cleanup(self, pair: WorkspacePair, *, keep_candidate_branch: bool = True) -> None:
        """Remove both worktrees. The fix branch is kept by default for Release."""

        self._safe_remove(pair.repo, Path(pair.control_workspace))
        self._safe_remove(pair.repo, Path(pair.candidate_workspace))
        if not keep_candidate_branch:
            try:
                self._git(pair.repo, "branch", "-D", pair.candidate_branch)
            except WorktreeError:
                pass
        run_dir = Path(pair.control_workspace).parent
        if run_dir.exists() and not any(run_dir.iterdir()):
            run_dir.rmdir()

    def _safe_remove(self, repo: str | Path, worktree_dir: Path) -> None:
        try:
            self._git(repo, "worktree", "remove", "--force", str(worktree_dir))
        except WorktreeError:
            shutil.rmtree(worktree_dir, ignore_errors=True)
            try:
                self._git(repo, "worktree", "prune")
            except WorktreeError:
                pass


def _safe_segment(value: str) -> str:
    if not value or any(sep in value for sep in ("/", "\\", "\x00", "..")):
        raise WorktreeError(f"unsafe run_id segment: {value!r}")
    return value


__all__ = [
    "DEFAULT_WORKTREE_IGNORE",
    "WorkspaceManager",
    "WorkspacePair",
    "WorktreeError",
]
