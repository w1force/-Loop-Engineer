"""Typed contracts for diagnosis-time log evidence discovery.

The diagnosis agent proposes *which source-backed log sites are useful*.  Trusted
code validates those sites, resolves them against the frozen control revision and
retrieves immutable log observations.  Retrieval scores are evidence, never a
release verdict.
"""

from __future__ import annotations

from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .base import Contract
from .incident import ArtifactReference, SourceLocation


_SHA256 = r"^[0-9a-f]{64}$"
_SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_EXTRACTABLE_IDENTIFIERS = {
    "trace_id",
    "request_id",
    "run_id",
    "session_id",
    "erp",
}


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class SignalRole(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class TemporalRelation(str, Enum):
    BEFORE_PRIMARY = "before_primary"
    AFTER_PRIMARY = "after_primary"
    EITHER = "either"


class ExpectedPresence(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    FORBIDDEN = "forbidden"


class EvidenceMatchStatus(str, Enum):
    EXACT_MATCH = "exact_match"
    FUZZY_FALLBACK = "fuzzy_fallback"
    NOT_FOUND = "not_found"
    CORRELATION_AMBIGUOUS = "correlation_ambiguous"
    COLLECTION_INCOMPLETE = "collection_incomplete"
    CONTEXT_OMITTED = "context_omitted"


class ReproductionDisposition(str, Enum):
    REPRODUCED = "reproduced"
    DUPLICATE = "duplicate"
    STALE_SIGNAL = "stale_signal"
    OLD_VERSION_SIGNAL = "old_version_signal"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    INVALID_REPRODUCER = "invalid_reproducer"
    NON_REPRODUCIBLE = "non_reproducible"
    NEW_INCIDENT = "new_incident"


class CorrelationIdentity(Contract):
    trace_id: str | None = None
    request_id: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    erp: str | None = None

    @field_validator("trace_id", "request_id", "run_id", "session_id", "erp")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if re.fullmatch(_SAFE_ID, value) is None:
            raise ValueError("correlation identity has an unsafe format")
        return value

    @property
    def strongest(self) -> tuple[str, str] | None:
        for name in ("trace_id", "request_id", "run_id", "session_id", "erp"):
            value = getattr(self, name)
            if value:
                return name, value
        return None

    @property
    def has_strong_id(self) -> bool:
        return any((self.trace_id, self.request_id, self.run_id, self.session_id))


class PrimarySignal(Contract):
    """Immutable reproduction target selected before evidence expansion."""

    schema_version: Literal["primary-signal/v1"] = "primary-signal/v1"
    event_id: str = Field(min_length=1)
    artifact: ArtifactReference
    observed_at: str = Field(min_length=1)
    timestamp_ns: int | None = Field(default=None, ge=0)
    service: str = Field(min_length=1)
    environment: str | None = None
    deployment_version: str | None = None
    instance_id: str | None = None
    thread_id: str | None = None
    task_id: str | None = None
    logger: str | None = None
    correlation: CorrelationIdentity = Field(default_factory=CorrelationIdentity)
    event_name: str | None = None
    error_type: str | None = None
    error_code: str | None = None
    message: str = ""
    message_template: str | None = None
    template_id: str | None = None
    original_input: Any = None

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class IncidentSignal(PrimarySignal):
    """A secondary immutable signal attached to the same causal incident."""

    schema_version: Literal["incident-signal/v1"] = "incident-signal/v1"
    role: Literal["secondary"] = "secondary"
    correlation_reason: str = Field(min_length=1)


class ProposedEvidenceLog(Contract):
    """One source-derived evidence request authored by the diagnosis planner."""

    evidence_id: str = Field(pattern=_SAFE_ID)
    priority: int = Field(ge=1, le=100)
    question: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=1000)
    source_path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)
    source_revision: str | None = None
    service: str | None = None
    logger: str | None = None
    level: str | None = None
    template: str = Field(min_length=1, max_length=4000)
    event_name: str | None = None
    error_code: str | None = None
    relation: TemporalRelation = TemporalRelation.EITHER
    max_time_delta_seconds: int = Field(default=60, ge=1, le=3600)
    expected_presence: ExpectedPresence = ExpectedPresence.OPTIONAL
    extract_fields: tuple[str, ...] = ()
    extract_patterns: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _valid_extraction(self):
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
        if len(self.extract_fields) != len(set(self.extract_fields)):
            raise ValueError("extract_fields must be unique")
        if set(self.extract_patterns) - set(self.extract_fields):
            raise ValueError("extract_patterns must be declared in extract_fields")
        unknown = (
            set(self.extract_fields) | set(self.extract_patterns)
        ) - _EXTRACTABLE_IDENTIFIERS
        if unknown:
            raise ValueError("unsupported extract field(s): " + ", ".join(sorted(unknown)))
        for pattern in self.extract_patterns.values():
            if len(pattern) > 512:
                raise ValueError("extract pattern exceeds 512 characters")
            if re.search(r"\\[1-9]|\(\?(?!P<value>)", pattern):
                raise ValueError(
                    "extract pattern cannot use backreferences or look-around"
                )
            if re.search(r"(?:\*|\+|\{\d+(?:,\d*)?\})\s*(?:\*|\+|\{)", pattern):
                raise ValueError("extract pattern contains nested quantifiers")
            compiled = re.compile(pattern)
            if "value" not in compiled.groupindex and compiled.groups != 1:
                raise ValueError(
                    "extract pattern requires one capture group or a named 'value' group"
                )
        return self


class EvidenceLogPlanProposal(Contract):
    schema_version: Literal["evidence-log-plan-proposal/v1"] = (
        "evidence-log-plan-proposal/v1"
    )
    evidence_logs: tuple[ProposedEvidenceLog, ...] = Field(
        min_length=1, max_length=50
    )

    @model_validator(mode="after")
    def _unique_ids(self):
        ids = [item.evidence_id for item in self.evidence_logs]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence_id values must be unique")
        return self


class FrozenEvidenceLog(Contract):
    evidence_id: str = Field(pattern=_SAFE_ID)
    priority: int = Field(ge=1, le=100)
    question: str
    reason: str
    source: SourceLocation
    source_sha256: str = Field(pattern=_SHA256)
    service: str
    logger: str | None = None
    level: str | None = None
    template: str
    template_id: str = Field(pattern=_SHA256)
    template_pattern: str
    event_name: str | None = None
    error_code: str | None = None
    relation: TemporalRelation
    max_time_delta_seconds: int
    expected_presence: ExpectedPresence
    extract_fields: tuple[str, ...] = ()
    extract_patterns: dict[str, str] = Field(default_factory=dict)

    @field_validator("template_pattern")
    @classmethod
    def _valid_template_pattern(cls, value: str) -> str:
        re.compile(value)
        return value


class EvidenceLogPlan(Contract):
    schema_version: Literal["evidence-log-plan/v1"] = "evidence-log-plan/v1"
    primary_signal_digest: str = Field(pattern=_SHA256)
    control_ref: str = Field(min_length=1)
    planner_skill_digest: str = Field(pattern=_SHA256)
    evidence_logs: tuple[FrozenEvidenceLog, ...] = Field(min_length=1, max_length=50)

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class EvidenceObservation(Contract):
    observation_id: str = Field(min_length=1)
    timestamp_ns: int = Field(ge=0)
    service: str = Field(min_length=1)
    level: str
    body: str
    raw_ref: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=_SHA256)
    match_mode: Literal["exact", "fuzzy"]
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    extracted_identity: CorrelationIdentity = Field(default_factory=CorrelationIdentity)
    truncated: bool = False
    original_tokens: int = Field(default=0, ge=0)
    included_ranges: tuple[tuple[int, int], ...] = ()


class EvidenceResult(Contract):
    evidence_id: str = Field(pattern=_SAFE_ID)
    priority: int = Field(ge=1, le=100)
    question: str = Field(min_length=1)
    source: SourceLocation
    expected_presence: ExpectedPresence
    status: EvidenceMatchStatus
    collection_complete: bool
    observations: tuple[EvidenceObservation, ...] = ()
    reason: str | None = None


class CorrelationProof(Contract):
    anchor_observation_id: str = Field(min_length=1)
    identity: CorrelationIdentity
    score: float = Field(ge=0.0, le=1.0)
    runner_up_margin: float = Field(ge=0.0, le=1.0)
    factors: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_identity(self):
        if self.identity.strongest is None:
            raise ValueError("correlation proof must extract an identity")
        return self


class DiagnosisEvidenceBundle(Contract):
    schema_version: Literal["diagnosis-evidence-bundle/v1"] = (
        "diagnosis-evidence-bundle/v1"
    )
    primary_signal_digest: str = Field(pattern=_SHA256)
    plan_digest: str = Field(pattern=_SHA256)
    collection_complete: bool
    correlation_proof: CorrelationProof | None = None
    results: tuple[EvidenceResult, ...]
    total_tokens: int = Field(ge=0)

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class ReproductionAssessment(Contract):
    attempt: int = Field(ge=1, le=3)
    disposition: ReproductionDisposition
    summary: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    hypothesis_digest: str = Field(pattern=_SHA256)


__all__ = [
    "CorrelationIdentity",
    "CorrelationProof",
    "DiagnosisEvidenceBundle",
    "EvidenceLogPlan",
    "EvidenceLogPlanProposal",
    "EvidenceMatchStatus",
    "EvidenceObservation",
    "EvidenceResult",
    "ExpectedPresence",
    "FrozenEvidenceLog",
    "IncidentSignal",
    "PrimarySignal",
    "ProposedEvidenceLog",
    "ReproductionAssessment",
    "ReproductionDisposition",
    "SignalRole",
    "TemporalRelation",
]
