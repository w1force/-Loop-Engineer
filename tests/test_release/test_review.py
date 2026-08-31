from __future__ import annotations

from typing import Any

import pytest

from core.learning.models import ReviewStatus
from core.release.github import ReleaseError
from core.release.models import PullRequestReceipt
from core.release.review import GitHubReviewClient, GitHubReviewResolver


VERIFIED_SHA = "a" * 40
OLD_SHA = "b" * 40


def _receipt(**overrides: Any) -> PullRequestReceipt:
    payload: dict[str, Any] = {
        "app_id": "checkout",
        "repository": "acme/checkout",
        "branch": "fix/timeout_20260831_1",
        "base_branch": "main",
        "commit_sha": VERIFIED_SHA,
        "pull_request_number": 42,
        "pull_request_url": "https://github.com/acme/checkout/pull/42",
        "reviewers": ("alice", "bob"),
        "verification_run_id": "run-42",
        "verification_cycle": 1,
        "verification_incident_id": "incident-42",
        "verification_incident_digest": "c" * 64,
        "candidate_digest": "d" * 64,
        "verification_report_digest": "e" * 64,
        "verification_report_path": "/evidence/run-42/report.json",
    }
    payload.update(overrides)
    return PullRequestReceipt(**payload)


def _pull_request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "number": 42,
        "state": "open",
        "merged": False,
        "draft": False,
        "base": {"ref": "main"},
        "head": {
            "ref": "fix/timeout_20260831_1",
            "sha": VERIFIED_SHA,
            "repo": {"full_name": "acme/checkout"},
        },
    }
    payload.update(overrides)
    return payload


def _review(
    *,
    review_id: int,
    reviewer: str,
    state: str,
    commit_id: str = VERIFIED_SHA,
    submitted_at: str = "2026-08-31T10:00:00Z",
    body: str | None = None,
) -> dict[str, Any]:
    return {
        "id": review_id,
        "user": {"login": reviewer},
        "state": state,
        "commit_id": commit_id,
        "submitted_at": submitted_at,
        "body": body,
    }


def _resolve(
    *reviews: dict[str, Any],
    receipt: PullRequestReceipt | None = None,
    pull_request: dict[str, Any] | None = None,
):
    return GitHubReviewResolver.resolve(
        receipt or _receipt(),
        pull_request=pull_request or _pull_request(),
        reviews=reviews,
    )


def test_review_fetch_rejects_pr_state_change_during_pagination() -> None:
    client = object.__new__(GitHubReviewClient)
    before = _pull_request()
    after = _pull_request()
    after["head"] = {**after["head"], "sha": OLD_SHA}
    responses = iter((before, [], after))
    client._get = lambda *args, **kwargs: next(responses)  # type: ignore[method-assign]

    with pytest.raises(ReleaseError, match="state changed"):
        client.fetch(repository="acme/checkout", pull_request_number=42)


def test_approved_review_must_match_exact_verified_head() -> None:
    decision = _resolve(
        _review(
            review_id=101,
            reviewer="Alice",
            state="APPROVED",
            body="ship it",
        )
    )

    assert decision.status is ReviewStatus.APPROVED
    assert decision.decision_source == "review"
    assert decision.reviewer == "Alice"
    assert decision.review_id == 101
    assert decision.commit_sha == VERIFIED_SHA
    assert decision.reason == "ship it"
    assert decision.payload_digest is not None


def test_approval_from_non_configured_reviewer_is_ignored() -> None:
    decision = _resolve(
        _review(review_id=102, reviewer="mallory", state="APPROVED")
    )

    assert decision.status is ReviewStatus.PENDING
    assert decision.reviewer is None
    assert decision.reason == "no current configured approval or rejection"


def test_draft_pull_request_cannot_be_approved() -> None:
    decision = _resolve(
        _review(review_id=103, reviewer="alice", state="APPROVED"),
        pull_request=_pull_request(draft=True),
    )

    assert decision.status is ReviewStatus.PENDING


def test_current_changes_requested_blocks_another_configured_approval() -> None:
    decision = _resolve(
        _review(
            review_id=104,
            reviewer="alice",
            state="APPROVED",
            submitted_at="2026-08-31T10:00:00Z",
        ),
        _review(
            review_id=105,
            reviewer="bob",
            state="CHANGES_REQUESTED",
            submitted_at="2026-08-31T10:05:00Z",
            body="needs a regression test",
        ),
    )

    assert decision.status is ReviewStatus.REJECTED
    assert decision.decision_source == "review"
    assert decision.reviewer == "bob"
    assert decision.review_id == 105
    assert decision.reason == "needs a regression test"


def test_closed_unmerged_pull_request_is_rejected() -> None:
    decision = _resolve(pull_request=_pull_request(state="closed", merged=False))

    assert decision.status is ReviewStatus.REJECTED
    assert decision.decision_source == "pull_request"
    assert decision.reason == "pull request was closed without merge"


def test_merged_pull_request_without_configured_approval_is_rejected() -> None:
    decision = _resolve(pull_request=_pull_request(state="closed", merged=True))

    assert decision.status is ReviewStatus.REJECTED
    assert decision.decision_source == "pull_request"
    assert decision.reason == "pull request merged without a configured approval"


def test_closed_unmerged_pull_request_with_head_drift_is_rejected() -> None:
    pull_request = _pull_request(state="closed", merged=False)
    pull_request["head"] = {**pull_request["head"], "sha": OLD_SHA}

    decision = _resolve(pull_request=pull_request)

    assert decision.status is ReviewStatus.REJECTED
    assert decision.reason == "pull request was closed without merge"


def test_merged_pull_request_with_head_drift_is_rejected() -> None:
    pull_request = _pull_request(state="closed", merged=True)
    pull_request["head"] = {**pull_request["head"], "sha": OLD_SHA}

    decision = _resolve(pull_request=pull_request)

    assert decision.status is ReviewStatus.REJECTED
    assert decision.reason == "pull request merged a head other than the verified commit"


def test_merged_pull_request_with_only_old_commit_review_is_rejected() -> None:
    decision = _resolve(
        _review(
            review_id=110,
            reviewer="alice",
            state="APPROVED",
            commit_id=OLD_SHA,
        ),
        pull_request=_pull_request(state="closed", merged=True),
    )

    assert decision.status is ReviewStatus.REJECTED
    assert decision.reason == "pull request merged without a configured approval"


def test_pull_request_head_drift_is_stale_even_with_approval() -> None:
    pull_request = _pull_request()
    pull_request["head"] = {**pull_request["head"], "sha": OLD_SHA}

    decision = _resolve(
        _review(review_id=106, reviewer="alice", state="APPROVED"),
        pull_request=pull_request,
    )

    assert decision.status is ReviewStatus.STALE_HEAD
    assert decision.decision_source == "system"
    assert decision.reason == "pull request head no longer matches the verified commit"


@pytest.mark.parametrize("state", ["APPROVED", "CHANGES_REQUESTED"])
def test_review_for_old_commit_is_stale(state: str) -> None:
    decision = _resolve(
        _review(
            review_id=107,
            reviewer="alice",
            state=state,
            commit_id=OLD_SHA,
        )
    )

    assert decision.status is ReviewStatus.STALE_HEAD
    assert decision.decision_source == "system"
    assert decision.reason == "configured review was submitted for another commit"


def test_later_dismissed_review_revokes_approval() -> None:
    decision = _resolve(
        _review(
            review_id=108,
            reviewer="alice",
            state="APPROVED",
            submitted_at="2026-08-31T10:00:00Z",
        ),
        _review(
            review_id=109,
            reviewer="alice",
            state="DISMISSED",
            submitted_at="2026-08-31T10:05:00Z",
        ),
    )

    assert decision.status is ReviewStatus.PENDING
    assert decision.reviewer is None


@pytest.mark.parametrize(
    ("pull_request", "error"),
    (
        (_pull_request(number=43), "PR number"),
        (_pull_request(base={"ref": "develop"}), "PR branch"),
        (
            _pull_request(
                head={
                    "ref": "fix/other_20260831_1",
                    "sha": VERIFIED_SHA,
                    "repo": {"full_name": "acme/checkout"},
                }
            ),
            "PR branch",
        ),
        (
            _pull_request(
                head={
                    "ref": "fix/timeout_20260831_1",
                    "sha": VERIFIED_SHA,
                    "repo": {"full_name": "fork/checkout"},
                }
            ),
            "head repository",
        ),
    ),
)
def test_pr_identity_branch_and_head_repository_must_match_receipt(
    pull_request: dict[str, Any], error: str
) -> None:
    with pytest.raises(ReleaseError, match=error):
        _resolve(pull_request=pull_request)
