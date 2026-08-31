"""Strict contracts for repair-trajectory learning."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _validate_skill_description(value: str | None) -> str | None:
    if value is None:
        return None
    if "<" in value or ">" in value:
        raise ValueError("skill description cannot contain angle brackets")
    if value.lstrip().startswith("[TODO:"):
        raise ValueError("skill description contains an unfinished TODO")
    return value


def _validate_skill_instructions(values: tuple[str, ...]) -> tuple[str, ...]:
    for value in values:
        if not value.strip():
            raise ValueError("skill instructions cannot contain empty entries")
        if value.lstrip().startswith("[TODO:"):
            raise ValueError("skill instructions contain an unfinished TODO")
    return values


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def repair_archive_payload_digest(
    *,
    incident: dict[str, Any],
    repair: dict[str, Any],
    candidate: dict[str, Any],
    verification_plan: dict[str, Any],
    replay_receipt: dict[str, Any],
    verification_report: dict[str, Any],
    prior_failures: tuple[str, ...],
) -> str:
    """Bind the complete redacted structured payload stored for one repair."""

    return canonical_digest(
        {
            "incident": incident,
            "repair": repair,
            "candidate": candidate,
            "verification_plan": verification_plan,
            "replay_receipt": replay_receipt,
            "verification_report": verification_report,
            "prior_failures": prior_failures,
        }
    )


class LearningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE_HEAD = "stale_head"


class HumanReviewDecision(LearningModel):
    schema_version: Literal["human-review-decision/v1"] = "human-review-decision/v1"
    run_id: str
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    pull_request_number: int = Field(gt=0)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    status: ReviewStatus
    decision_source: Literal["review", "pull_request", "system"] = "review"
    reviewer: str | None = None
    review_id: int | None = Field(default=None, gt=0)
    reason: str | None = None
    submitted_at: str | None = None
    payload_digest: str | None = Field(default=None, pattern=_SHA256.pattern)

    @field_validator("run_id")
    @classmethod
    def _safe_run_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("run_id is not a safe identifier")
        return value

    @model_validator(mode="after")
    def _decisive_review_has_identity(self) -> Self:
        if self.status is ReviewStatus.APPROVED:
            if not self.reviewer or self.review_id is None:
                raise ValueError("approved review requires reviewer and review_id")
        if self.status is ReviewStatus.REJECTED and self.decision_source == "review":
            if not self.reviewer or self.review_id is None:
                raise ValueError("rejected review requires reviewer and review_id")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class ShareGPTTrajectory(LearningModel):
    """ShareGPT-compatible repair conversation plus lifecycle metadata."""

    schema_version: Literal["repair-sharegpt/v1"] = "repair-sharegpt/v1"
    run_id: str
    incident_id: str
    stage: Literal["repair"] = "repair"
    cycle: int = Field(ge=1, le=3)
    conversations: tuple[dict[str, Any], ...] = Field(min_length=1)
    timestamp: str = Field(default_factory=utc_now)
    model: str = Field(min_length=1)
    completed: bool
    terminal_reason: str = Field(min_length=1)
    trace_path: str | None = None
    tools: tuple[dict[str, Any], ...] = ()
    reasoning_blocks: tuple[dict[str, Any], ...] = ()

    @field_validator("run_id", "incident_id")
    @classmethod
    def _safe_ids(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("trajectory identifier is unsafe")
        return value

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))

    @property
    def has_usable_reasoning(self) -> bool:
        return any(
            block.get("type") == "thinking"
            and isinstance(block.get("thinking"), str)
            and bool(block["thinking"].strip())
            for block in self.reasoning_blocks
        )


class PendingRepairTrajectory(LearningModel):
    """Hard-VERIFIED trajectory waiting for a human MR decision."""

    schema_version: Literal["pending-repair-trajectory/v3"] = (
        "pending-repair-trajectory/v3"
    )
    run_id: str
    incident_id: str
    cycle: int = Field(ge=1, le=3)
    trajectory_path: str | None = None
    trajectory_digest: str | None = Field(default=None, pattern=_SHA256.pattern)
    incident_digest: str = Field(pattern=_SHA256.pattern)
    plan_digest: str = Field(pattern=_SHA256.pattern)
    replay_digest: str = Field(pattern=_SHA256.pattern)
    report_digest: str = Field(pattern=_SHA256.pattern)
    archive_payload_digest: str = Field(pattern=_SHA256.pattern)
    incident: dict[str, Any]
    repair: dict[str, Any]
    candidate: dict[str, Any]
    verification_plan: dict[str, Any]
    replay_receipt: dict[str, Any]
    verification_report: dict[str, Any]
    prior_failures: tuple[str, ...] = ()
    release_receipt: dict[str, Any] | None = None
    captured_at: str = Field(default_factory=utc_now)

    @field_validator("run_id", "incident_id")
    @classmethod
    def _safe_pending_ids(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("pending trajectory identifier is unsafe")
        return value

    @model_validator(mode="after")
    def _must_be_verified(self) -> Self:
        if self.verification_report.get("verdict") != "verified":
            raise ValueError("pending learning trajectory requires a VERIFIED report")
        if self.verification_report.get("run_id") != self.run_id:
            raise ValueError("verification report run_id mismatch")
        if self.verification_report.get("incident_id") != self.incident_id:
            raise ValueError("verification report incident_id mismatch")
        expected = {
            "cycle": self.cycle,
            "incident_digest": self.incident_digest,
            "plan_digest": self.plan_digest,
            "replay_digest": self.replay_digest,
            "candidate_digest": self.candidate.get("candidate_digest"),
        }
        mismatches = [
            key
            for key, value in expected.items()
            if self.verification_report.get(key) != value
        ]
        if mismatches:
            raise ValueError(
                "verification report binding mismatch: "
                + ", ".join(sorted(mismatches))
            )
        if self.trajectory_digest is not None and self.trajectory_path is None:
            raise ValueError("trajectory_digest requires trajectory_path")
        actual_archive_digest = repair_archive_payload_digest(
            incident=self.incident,
            repair=self.repair,
            candidate=self.candidate,
            verification_plan=self.verification_plan,
            replay_receipt=self.replay_receipt,
            verification_report=self.verification_report,
            prior_failures=self.prior_failures,
        )
        if actual_archive_digest != self.archive_payload_digest:
            raise ValueError("archived repair payload digest mismatch")
        return self


class CompressedRepairTrajectory(LearningModel):
    schema_version: Literal["compressed-repair-trajectory/v2"] = (
        "compressed-repair-trajectory/v2"
    )
    run_id: str
    incident_id: str
    cycle: int = Field(ge=1, le=3)
    review: HumanReviewDecision
    source_trajectory_digest: str = Field(pattern=_SHA256.pattern)
    conversations: tuple[dict[str, Any], ...] = Field(min_length=1)
    loaded_skill_names: tuple[str, ...] = ()
    reasoning_blocks: tuple[dict[str, Any], ...] = ()
    reasoning_block_count: int = Field(ge=0)
    usable_reasoning_count: int = Field(ge=0)
    source_completed: bool
    terminal_reason: str = Field(min_length=1)
    compressed: bool
    summary: str | None = None
    original_tokens: int = Field(ge=0)
    compressed_tokens: int = Field(ge=0)
    tokenizer_name: str = Field(min_length=1)
    summarization_model: str = Field(min_length=1)
    created_at: str = Field(default_factory=utc_now)

    @property
    def eligible_for_skill(self) -> bool:
        return self.source_completed and self.usable_reasoning_count > 0


class SkillMatch(LearningModel):
    matched_rule: str = Field(min_length=1)
    signature_code: str = Field(min_length=1)
    error_type: str | None = None
    event_code: str | None = None
    message_pattern: str | None = None
    source_paths: tuple[str, ...] = Field(min_length=1)


class SkillProvenance(LearningModel):
    run_id: str
    incident_id: str
    candidate_digest: str = Field(pattern=_SHA256.pattern)
    report_digest: str = Field(pattern=_SHA256.pattern)
    review_digest: str = Field(pattern=_SHA256.pattern)
    compressed_trajectory_path: str = Field(min_length=1)


class ExperienceSkill(LearningModel):
    """A reusable, advisory Skill for Diagnosis and Repair only."""

    schema_version: Literal["diagnose-repair-experience/v1"] = (
        "diagnose-repair-experience/v1"
    )
    name: str = Field(
        max_length=64,
        pattern=r"^learned-repair-[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    description: str = Field(min_length=1, max_length=500)
    match: SkillMatch
    applicable_when: tuple[str, ...] = Field(min_length=1)
    diagnosis_steps: tuple[str, ...] = Field(min_length=1)
    repair_steps: tuple[str, ...] = Field(min_length=1)
    pitfalls: tuple[str, ...] = ()
    provenance: tuple[SkillProvenance, ...] = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @field_validator("description")
    @classmethod
    def _safe_description(cls, value: str) -> str:
        return _validate_skill_description(value) or ""

    @field_validator(
        "applicable_when", "diagnosis_steps", "repair_steps", "pitfalls"
    )
    @classmethod
    def _safe_instructions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_skill_instructions(values)


class SkillMutationProposal(LearningModel):
    """Strict LLM output. The host validates and performs the write."""

    action: Literal["create", "update", "noop"]
    target_name: str | None = None
    rationale: str = Field(min_length=1, max_length=1000)
    description: str | None = Field(default=None, max_length=500)
    applicable_when: tuple[str, ...] = ()
    diagnosis_steps: tuple[str, ...] = ()
    repair_steps: tuple[str, ...] = ()
    pitfalls: tuple[str, ...] = ()

    @field_validator("description")
    @classmethod
    def _safe_description(cls, value: str | None) -> str | None:
        return _validate_skill_description(value)

    @field_validator(
        "applicable_when", "diagnosis_steps", "repair_steps", "pitfalls"
    )
    @classmethod
    def _safe_instructions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_skill_instructions(values)

    @model_validator(mode="after")
    def _action_contract(self) -> Self:
        if self.action == "update" and not self.target_name:
            raise ValueError("update requires target_name")
        if self.action == "create" and self.target_name is not None:
            raise ValueError("create target_name is assigned by the host")
        if self.action != "noop":
            required = (
                self.description,
                self.applicable_when,
                self.diagnosis_steps,
                self.repair_steps,
            )
            if not all(required):
                raise ValueError("create/update requires complete skill content")
        return self


class LearningResult(LearningModel):
    run_id: str
    review_status: ReviewStatus
    archive_path: str | None = None
    action: Literal["pending", "archived_failed", "created", "updated", "noop"]
    skill_name: str | None = None
    skill_digest: str | None = Field(default=None, pattern=_SHA256.pattern)
    reason: str | None = None
