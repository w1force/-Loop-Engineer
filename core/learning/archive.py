"""Durable filesystem archive for pending and reviewed repair trajectories."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, AsyncIterator, Iterator

from .models import (
    CompressedRepairTrajectory,
    HumanReviewDecision,
    LearningResult,
    PendingRepairTrajectory,
    ReviewStatus,
    ShareGPTTrajectory,
    canonical_digest,
)
from .trajectory import load_sharegpt_trajectory, redact_sensitive


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


@contextmanager
def _artifact_lock(path: Path) -> Iterator[None]:
    """Serialize check-and-publish for artifacts in the same directory."""

    lock_path = path.parent / ".archive.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_json(
    path: Path,
    payload: dict[str, Any],
    *,
    replace: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_digest = canonical_digest(payload)

    with _artifact_lock(path):
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise FileExistsError(
                    f"learning artifact target cannot be a symlink: {path}"
                )
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                raise FileExistsError(
                    f"existing learning artifact is unreadable: {path}"
                ) from exc
            existing_digest = canonical_digest(existing)
            if existing_digest == payload_digest:
                return
            if not replace:
                raise FileExistsError(
                    f"different learning artifact already exists: {path}"
                )

        fd, temporary_name = tempfile.mkstemp(prefix=".learning-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def _atomic_json_line(path: Path, payload: dict[str, Any]) -> None:
    """Write one immutable JSONL record with the same concurrency guarantees."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload_digest = canonical_digest(payload)
    with _artifact_lock(path):
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise FileExistsError(
                    f"learning artifact target cannot be a symlink: {path}"
                )
            try:
                existing = load_sharegpt_trajectory(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise FileExistsError(
                    f"existing learning artifact is unreadable: {path}"
                ) from exc
            if canonical_digest(existing.model_dump(mode="json")) == payload_digest:
                return
            raise FileExistsError(f"different learning artifact already exists: {path}")

        fd, temporary_name = tempfile.mkstemp(prefix=".trajectory-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


class RepairTrajectoryArchive:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def _run_path(self, category: str, run_id: str, suffix: str = ".json") -> Path:
        if not _SAFE_ID.fullmatch(run_id):
            raise ValueError("unsafe learning run_id")
        target = (self.root / category / f"{run_id}{suffix}").resolve()
        target.relative_to(self.root)
        return target

    @asynccontextmanager
    async def run_lock(self, run_id: str) -> AsyncIterator[None]:
        """Serialize one run across service instances and worker processes."""

        lock_path = self._run_path("run-locks", run_id, suffix=".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        acquired = False
        try:
            os.fchmod(fd, 0o600)
            while not acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            yield
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def save_pending(self, pending: PendingRepairTrajectory) -> str:
        target = self._run_path("pending", pending.run_id)
        if target.is_file():
            existing = PendingRepairTrajectory.model_validate_json(target.read_bytes())
            excluded = {
                "captured_at",
                "release_receipt",
                "trajectory_path",
                "trajectory_digest",
            }
            old = existing.model_dump(mode="json", exclude=excluded)
            new = pending.model_dump(mode="json", exclude=excluded)
            if old == new:
                return str(target)

        trajectory = self._load_or_recover_sharegpt(pending)
        if (
            trajectory.run_id != pending.run_id
            or trajectory.incident_id != pending.incident_id
            or trajectory.cycle != pending.cycle
        ):
            raise ValueError("ShareGPT trajectory identity does not match pending repair")
        trajectory = ShareGPTTrajectory.model_validate(
            redact_sensitive(trajectory.model_dump(mode="json"))
        )
        trajectory_target = self._run_path(
            "trajectories", pending.run_id, suffix=".sharegpt.jsonl"
        )
        _atomic_json_line(
            trajectory_target,
            trajectory.model_dump(mode="json"),
        )
        frozen = pending.model_copy(
            update={
                "trajectory_path": str(trajectory_target),
                "trajectory_digest": trajectory.digest,
            }
        )
        _atomic_json(target, frozen.model_dump(mode="json"), replace=False)
        return str(target)

    @staticmethod
    def _fallback_sharegpt(pending: PendingRepairTrajectory) -> ShareGPTTrajectory:
        # The head keeps the Diagnosis result even when the provider transcript was
        # unavailable. With no visible reasoning this remains archive-only.
        return ShareGPTTrajectory(
            run_id=pending.run_id,
            incident_id=pending.incident_id,
            cycle=pending.cycle,
            model="unknown",
            completed=True,
            terminal_reason="completed",
            conversations=(
                {
                    "from": "system",
                    "value": "Recovered repair trajectory from trusted structured artifacts.",
                },
                {
                    "from": "human",
                    "value": "INCIDENT_JSON:\n"
                    + json.dumps(pending.incident, ensure_ascii=False, sort_keys=True),
                },
                {
                    "from": "gpt",
                    "value": json.dumps(pending.repair, ensure_ascii=False, sort_keys=True),
                },
            ),
        )

    @classmethod
    def _load_or_recover_sharegpt(
        cls, pending: PendingRepairTrajectory
    ) -> ShareGPTTrajectory:
        if pending.trajectory_path is None:
            return cls._fallback_sharegpt(pending)
        # A declared trajectory must be readable and valid. Silent fallback would
        # turn file drift or corruption into a different learning sample.
        return load_sharegpt_trajectory(pending.trajectory_path)

    def load_pending(self, run_id: str) -> PendingRepairTrajectory:
        target = self._run_path("pending", run_id)
        return PendingRepairTrajectory.model_validate_json(target.read_bytes())

    def list_pending(self) -> tuple[PendingRepairTrajectory, ...]:
        directory = self.root / "pending"
        if not directory.is_dir() or directory.is_symlink():
            return ()
        pending: list[PendingRepairTrajectory] = []
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                item = PendingRepairTrajectory.model_validate_json(path.read_bytes())
            except (OSError, ValueError):
                continue
            if self.load_terminal_result(item.run_id) is None:
                pending.append(item)
        return tuple(pending)

    def bind_release(self, run_id: str, receipt: dict[str, Any]) -> str:
        pending = self.load_pending(run_id)
        expected = {
            "verification_run_id": run_id,
            "verification_cycle": pending.cycle,
            "verification_incident_id": pending.incident_id,
            "verification_incident_digest": pending.incident_digest,
            "candidate_digest": pending.candidate.get("candidate_digest"),
            "verification_report_digest": pending.report_digest,
        }
        mismatches = [
            key for key, value in expected.items() if receipt.get(key) != value
        ]
        if mismatches:
            raise ValueError(
                "release receipt does not match pending repair: "
                + ", ".join(sorted(mismatches))
            )
        receipt_target = self._run_path("release-receipts", run_id)
        _atomic_json(receipt_target, receipt, replace=False)
        existing = pending.release_receipt
        if existing is not None and canonical_digest(existing) != canonical_digest(receipt):
            raise ValueError("pending trajectory already has a different release receipt")
        updated = pending.model_copy(update={"release_receipt": receipt})
        target = self._run_path("pending", run_id)
        _atomic_json(target, updated.model_dump(mode="json"), replace=True)
        return str(target)

    def load_release_receipt(self, run_id: str) -> dict[str, Any] | None:
        target = self._run_path("release-receipts", run_id)
        if not target.exists():
            return None
        if target.is_symlink() or not target.is_file():
            raise ValueError("release receipt artifact is not a regular file")
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError("release receipt artifact is unreadable") from exc
        if not isinstance(payload, dict):
            raise ValueError("release receipt artifact must be a JSON object")
        if payload.get("verification_run_id") != run_id:
            raise ValueError("release receipt artifact run_id mismatch")
        return payload

    def load_sharegpt(self, pending: PendingRepairTrajectory) -> ShareGPTTrajectory:
        if pending.trajectory_path is None or pending.trajectory_digest is None:
            raise ValueError("pending repair has no frozen ShareGPT trajectory")
        source = Path(pending.trajectory_path)
        resolved = source.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("pending trajectory is outside the learning archive") from exc
        if source.is_symlink() or not resolved.is_file():
            raise ValueError("frozen pending trajectory is missing or is a symlink")
        trajectory = load_sharegpt_trajectory(resolved)
        if trajectory.digest != pending.trajectory_digest:
            raise ValueError("frozen pending trajectory digest mismatch")
        if (
            trajectory.run_id != pending.run_id
            or trajectory.incident_id != pending.incident_id
            or trajectory.cycle != pending.cycle
        ):
            raise ValueError("frozen pending trajectory identity mismatch")
        return trajectory

    def save_compressed(self, compressed: CompressedRepairTrajectory) -> str:
        target = Path(self.compressed_path(compressed.review))
        _atomic_json(target, compressed.model_dump(mode="json"), replace=False)
        return str(target)

    def compressed_path(self, review: HumanReviewDecision) -> str:
        category = "approved" if review.status.value == "approved" else "rejected"
        return str(
            self._run_path(
                f"reviewed/{category}",
                review.run_id,
                suffix=".json",
            )
        )

    def load_compressed(
        self, review: HumanReviewDecision
    ) -> CompressedRepairTrajectory | None:
        target = Path(self.compressed_path(review))
        if not target.is_file():
            return None
        compressed = CompressedRepairTrajectory.model_validate_json(target.read_bytes())
        stored = compressed.review
        identity = (
            "run_id",
            "repository",
            "pull_request_number",
            "commit_sha",
            "status",
        )
        if any(getattr(stored, field) != getattr(review, field) for field in identity):
            raise ValueError("compressed trajectory belongs to another review decision")
        return compressed

    def save_review(self, review: HumanReviewDecision) -> str:
        target = self._run_path(
            "reviews",
            review.run_id,
            suffix=f"-{review.digest[:16]}.json",
        )
        _atomic_json(target, review.model_dump(mode="json"), replace=False)
        return str(target)

    def result_path(self, run_id: str, review_digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", review_digest):
            raise ValueError("invalid review digest")
        return self._run_path("results", run_id, suffix=f"-{review_digest[:16]}.json")

    def load_result(self, run_id: str, review_digest: str) -> LearningResult | None:
        target = self.result_path(run_id, review_digest)
        if not target.is_file():
            return None
        return LearningResult.model_validate_json(target.read_bytes())

    def save_result(self, result: LearningResult, review_digest: str) -> str:
        target = self.result_path(result.run_id, review_digest)
        _atomic_json(target, result.model_dump(mode="json"), replace=False)
        return str(target)

    def terminal_result_path(self, run_id: str) -> Path:
        return self._run_path("results", run_id, suffix="-terminal.json")

    def load_terminal_result(self, run_id: str) -> LearningResult | None:
        target = self.terminal_result_path(run_id)
        if not target.is_file():
            return None
        return LearningResult.model_validate_json(target.read_bytes())

    def save_terminal_result(self, result: LearningResult) -> str:
        if result.review_status not in {
            ReviewStatus.APPROVED,
            ReviewStatus.REJECTED,
            ReviewStatus.STALE_HEAD,
        }:
            raise ValueError("only a final review outcome can be terminal")
        target = self.terminal_result_path(result.run_id)
        _atomic_json(target, result.model_dump(mode="json"), replace=False)
        return str(target)


__all__ = ["RepairTrajectoryArchive"]
