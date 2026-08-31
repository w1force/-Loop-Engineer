"""GitHub human-review polling and exact-head decision binding."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from core.learning.models import (
    HumanReviewDecision,
    LearningResult,
    ReviewStatus,
    canonical_digest,
)

from .github import ReleaseError
from .models import PullRequestReceipt


class ReviewLearningSink(Protocol):
    async def process_review(
        self, decision: HumanReviewDecision
    ) -> LearningResult: ...


class GitHubReviewClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        if not token:
            raise ReleaseError("缺少 GitHub token")
        parsed = urlparse(api_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ReleaseError("GitHub API URL 端口非法") from exc
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "api.github.com"
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ReleaseError("GitHub API URL 必须是 https://api.github.com")
        self.client = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
            trust_env=False,
        )

    def _get(self, path: str, **kwargs) -> Any:
        response = self.client.get(path, **kwargs)
        if response.status_code >= 400:
            raise ReleaseError(
                f"GitHub API GET {path} 失败: HTTP {response.status_code} "
                f"{response.text[:500]}"
            )
        return response.json()

    def fetch(
        self, *, repository: str, pull_request_number: int
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
        prefix = f"/repos/{repository}/pulls/{pull_request_number}"
        pull_request_before = self._get(prefix)
        if not isinstance(pull_request_before, dict):
            raise ReleaseError("GitHub pull request 响应格式非法")
        reviews: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._get(
                prefix + "/reviews", params={"per_page": 100, "page": page}
            )
            if not isinstance(batch, list):
                raise ReleaseError("GitHub review 响应格式非法")
            reviews.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
            page += 1
        pull_request = self._get(prefix)
        if not isinstance(pull_request, dict):
            raise ReleaseError("GitHub pull request 响应格式非法")
        if self._decision_snapshot(pull_request_before) != self._decision_snapshot(
            pull_request
        ):
            raise ReleaseError("GitHub PR state changed while reviews were fetched")
        return pull_request, tuple(reviews)

    @staticmethod
    def _decision_snapshot(pull_request: dict[str, Any]) -> tuple[Any, ...]:
        base = pull_request.get("base") or {}
        head = pull_request.get("head") or {}
        return (
            pull_request.get("number"),
            pull_request.get("state"),
            pull_request.get("merged"),
            pull_request.get("draft"),
            base.get("ref"),
            head.get("ref"),
            head.get("sha"),
            (head.get("repo") or {}).get("full_name"),
        )

    def close(self) -> None:
        self.client.close()


class GitHubReviewResolver:
    """Resolve GitHub's review history into one trusted lifecycle decision."""

    @staticmethod
    def resolve(
        receipt: PullRequestReceipt,
        *,
        pull_request: dict[str, Any],
        reviews: Sequence[dict[str, Any]],
    ) -> HumanReviewDecision:
        receipt = PullRequestReceipt.model_validate_json(receipt.model_dump_json())
        if int(pull_request.get("number") or 0) != receipt.pull_request_number:
            raise ReleaseError("GitHub PR number 与 release receipt 不一致")
        base = pull_request.get("base") or {}
        head = pull_request.get("head") or {}
        if base.get("ref") != receipt.base_branch or head.get("ref") != receipt.branch:
            raise ReleaseError("GitHub PR branch 与 release receipt 不一致")
        head_repository = (head.get("repo") or {}).get("full_name")
        if head_repository != receipt.repository:
            raise ReleaseError("GitHub PR head repository 与 release receipt 不一致")

        payload_digest = canonical_digest(
            {"pull_request": pull_request, "reviews": list(reviews)}
        )
        state = str(pull_request.get("state") or "").lower()
        merged = bool(pull_request.get("merged"))
        # A closed PR is terminal for this receipt. Check it before head drift so
        # a force-pushed-and-closed PR does not remain in the pending queue forever.
        if state == "closed" and not merged:
            return HumanReviewDecision(
                run_id=receipt.verification_run_id,
                repository=receipt.repository,
                pull_request_number=receipt.pull_request_number,
                commit_sha=receipt.commit_sha,
                status=ReviewStatus.REJECTED,
                decision_source="pull_request",
                reason="pull request was closed without merge",
                payload_digest=payload_digest,
            )
        if merged and head.get("sha") != receipt.commit_sha:
            return HumanReviewDecision(
                run_id=receipt.verification_run_id,
                repository=receipt.repository,
                pull_request_number=receipt.pull_request_number,
                commit_sha=receipt.commit_sha,
                status=ReviewStatus.REJECTED,
                decision_source="pull_request",
                reason="pull request merged a head other than the verified commit",
                payload_digest=payload_digest,
            )
        if head.get("sha") != receipt.commit_sha:
            return HumanReviewDecision(
                run_id=receipt.verification_run_id,
                repository=receipt.repository,
                pull_request_number=receipt.pull_request_number,
                commit_sha=receipt.commit_sha,
                status=ReviewStatus.STALE_HEAD,
                decision_source="system",
                reason="pull request head no longer matches the verified commit",
                payload_digest=payload_digest,
            )

        configured = {item.lower() for item in receipt.reviewers}
        latest: dict[str, dict[str, Any]] = {}
        old_commit_decision = False
        for review in reviews:
            user = review.get("user") or {}
            login = user.get("login") if isinstance(user, dict) else None
            if not isinstance(login, str) or login.lower() not in configured:
                continue
            review_state = review.get("state")
            if review_state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                continue
            if review.get("commit_id") != receipt.commit_sha:
                if review_state in {"APPROVED", "CHANGES_REQUESTED"}:
                    old_commit_decision = True
                continue
            review_id = review.get("id")
            if not isinstance(review_id, int) or review_id < 1:
                continue
            key = login.lower()
            previous = latest.get(key)
            if previous is None or (
                str(review.get("submitted_at") or ""), review_id
            ) > (
                str(previous.get("submitted_at") or ""), int(previous["id"])
            ):
                latest[key] = review

        rejected = [
            item
            for item in latest.values()
            if item.get("state") == "CHANGES_REQUESTED"
        ]
        if rejected:
            selected = max(
                rejected,
                key=lambda item: (str(item.get("submitted_at") or ""), int(item["id"])),
            )
            return GitHubReviewResolver._from_review(
                receipt,
                selected,
                status=ReviewStatus.REJECTED,
                payload_digest=payload_digest,
            )

        approvals = [
            item for item in latest.values() if item.get("state") == "APPROVED"
        ]
        if approvals and not bool(pull_request.get("draft")):
            selected = max(
                approvals,
                key=lambda item: (str(item.get("submitted_at") or ""), int(item["id"])),
            )
            return GitHubReviewResolver._from_review(
                receipt,
                selected,
                status=ReviewStatus.APPROVED,
                payload_digest=payload_digest,
            )

        if merged:
            return HumanReviewDecision(
                run_id=receipt.verification_run_id,
                repository=receipt.repository,
                pull_request_number=receipt.pull_request_number,
                commit_sha=receipt.commit_sha,
                status=ReviewStatus.REJECTED,
                decision_source="pull_request",
                reason="pull request merged without a configured approval",
                payload_digest=payload_digest,
            )

        if old_commit_decision:
            return HumanReviewDecision(
                run_id=receipt.verification_run_id,
                repository=receipt.repository,
                pull_request_number=receipt.pull_request_number,
                commit_sha=receipt.commit_sha,
                status=ReviewStatus.STALE_HEAD,
                decision_source="system",
                reason="configured review was submitted for another commit",
                payload_digest=payload_digest,
            )

        return HumanReviewDecision(
            run_id=receipt.verification_run_id,
            repository=receipt.repository,
            pull_request_number=receipt.pull_request_number,
            commit_sha=receipt.commit_sha,
            status=ReviewStatus.PENDING,
            decision_source="system",
            reason="no current configured approval or rejection",
            payload_digest=payload_digest,
        )

    @staticmethod
    def _from_review(
        receipt: PullRequestReceipt,
        review: dict[str, Any],
        *,
        status: ReviewStatus,
        payload_digest: str,
    ) -> HumanReviewDecision:
        return HumanReviewDecision(
            run_id=receipt.verification_run_id,
            repository=receipt.repository,
            pull_request_number=receipt.pull_request_number,
            commit_sha=receipt.commit_sha,
            status=status,
            decision_source="review",
            reviewer=str((review.get("user") or {})["login"]),
            review_id=int(review["id"]),
            reason=(str(review.get("body"))[:1000] if review.get("body") else None),
            submitted_at=(
                str(review.get("submitted_at"))
                if review.get("submitted_at")
                else None
            ),
            payload_digest=payload_digest,
        )


class GitHubReviewMonitor:
    def __init__(
        self, *, client: GitHubReviewClient, learning_sink: ReviewLearningSink
    ) -> None:
        self.client = client
        self.learning_sink = learning_sink

    async def poll(
        self, receipt: PullRequestReceipt
    ) -> tuple[HumanReviewDecision, LearningResult]:
        pull_request, reviews = await asyncio.to_thread(
            self.client.fetch,
            repository=receipt.repository,
            pull_request_number=receipt.pull_request_number,
        )
        decision = GitHubReviewResolver.resolve(
            receipt, pull_request=pull_request, reviews=reviews
        )
        result = await self.learning_sink.process_review(decision)
        return decision, result


__all__ = [
    "GitHubReviewClient",
    "GitHubReviewMonitor",
    "GitHubReviewResolver",
]
