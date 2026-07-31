"""诊断审查策略与结构化审查结果。"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ReviewMode(str, Enum):
    REQUIRED = "required"
    DISABLED = "disabled"


class UnresolvedReviewAction(str, Enum):
    DOWNGRADE = "downgrade"
    FAIL = "fail"


class DiagnosisReviewPolicy(BaseModel):
    mode: ReviewMode = ReviewMode.REQUIRED
    max_rework_rounds: int = Field(default=1, ge=0, le=5)
    unresolved_action: UnresolvedReviewAction = UnresolvedReviewAction.DOWNGRADE
    allow_reviewer_mcp: bool = True


class ReviewDecision(str, Enum):
    APPROVED = "approved"
    REVISION_REQUIRED = "revision_required"


class ProposalReviewVerdict(str, Enum):
    APPROVE = "approve"
    REVISE = "revise"
    REJECT = "reject"


class ReviewSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ReviewFinding(BaseModel):
    code: str
    severity: ReviewSeverity
    target_type: Literal["claim_proposal", "hypothesis", "evidence", "diagnosis"]
    target_id: str | None = None
    message: str
    evidence_ids: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)


class ProposalReview(BaseModel):
    proposal_id: str
    verdict: ProposalReviewVerdict
    rationale: str
    finding_codes: list[str] = Field(default_factory=list)


class DiagnosisReview(BaseModel):
    reviewed_revision: int = Field(ge=0)
    decision: ReviewDecision
    proposal_reviews: list[ProposalReview]
    findings: list[ReviewFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def decision_matches_findings(self) -> "DiagnosisReview":
        has_blocker = any(
            review.verdict != ProposalReviewVerdict.APPROVE
            for review in self.proposal_reviews
        ) or any(finding.severity == ReviewSeverity.ERROR for finding in self.findings)
        if self.decision == ReviewDecision.APPROVED and has_blocker:
            raise ValueError("approved review cannot contain blocking findings or verdicts")
        if self.decision == ReviewDecision.REVISION_REQUIRED and not has_blocker:
            raise ValueError("revision_required review must contain a blocking finding or verdict")
        return self


class ReviewCycle(BaseModel):
    round_index: int = Field(ge=0)
    review: DiagnosisReview
