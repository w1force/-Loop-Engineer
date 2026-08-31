"""One-shot worker that turns GitHub review decisions into learning outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .archive import RepairTrajectoryArchive

if TYPE_CHECKING:
    from core.release.review import GitHubReviewMonitor


@dataclass(frozen=True)
class ReviewPollSummary:
    checked: int
    pending: int
    finalized: int
    failures: tuple[str, ...] = ()


class PendingReviewWorker:
    def __init__(
        self,
        *,
        archive: RepairTrajectoryArchive,
        monitor: "GitHubReviewMonitor",
        receipt_roots: tuple[str | Path, ...] = (),
    ) -> None:
        self.archive = archive
        self.monitor = monitor
        self.receipt_roots = tuple(Path(item).expanduser().resolve() for item in receipt_roots)

    def _recover_release_receipt(self, run_id: str):
        from core.release.models import PullRequestReceipt

        matches: list[PullRequestReceipt] = []
        archived = self.archive.load_release_receipt(run_id)
        if archived is not None:
            matches.append(PullRequestReceipt.model_validate(archived))
        external_match = False
        for root in self.receipt_roots:
            target = (root / f"{run_id}.json").resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if target.is_symlink() or not target.is_file():
                continue
            receipt = PullRequestReceipt.model_validate_json(target.read_bytes())
            if receipt.verification_run_id != run_id:
                raise ValueError("release receipt run_id does not match its filename")
            external_match = True
            if receipt not in matches:
                matches.append(receipt)
        if self.receipt_roots and not external_match:
            raise ValueError("release receipt is missing from configured operator roots")
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError("conflicting release receipts found for one learning run")
        receipt = matches[0]
        self.archive.bind_release(run_id, receipt.model_dump(mode="json"))
        return receipt

    async def poll_once(self) -> ReviewPollSummary:
        checked = pending_count = finalized = 0
        failures: list[str] = []
        for item in self.archive.list_pending():
            checked += 1
            try:
                # Always reconcile configured operator-owned receipt roots, even
                # when the learning archive already embeds a receipt copy.
                receipt = self._recover_release_receipt(item.run_id)
                if receipt is None:
                    pending_count += 1
                    failures.append(
                        f"{item.run_id}: pending trajectory has no bound release receipt"
                    )
                    continue
                decision, _ = await self.monitor.poll(receipt)
            except Exception as exc:  # isolate one PR from the remaining queue
                failures.append(f"{item.run_id}: {type(exc).__name__}: {exc}")
                continue
            if decision.status.value in {"approved", "rejected", "stale_head"}:
                finalized += 1
            else:
                pending_count += 1
        return ReviewPollSummary(
            checked=checked,
            pending=pending_count,
            finalized=finalized,
            failures=tuple(failures),
        )


__all__ = ["PendingReviewWorker", "ReviewPollSummary"]
