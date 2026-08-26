"""Untrusted structured output of the read-only Diagnosis stage.

The diagnosis agent produces a ``DiagnosisProposal`` — deliberately lenient because
it is agent-authored. The trusted ``IncidentFreezer``
(``core.stages.diagnosis.freezer``) validates it against the frozen control ref and
the trusted rule resolver before minting an ``IncidentBundle`` for later stages.
Analysis is an internal step of diagnosis, not a separate artifact.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .base import Contract


class ProposedSourceLocation(Contract):
    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)
    revision: str = Field(min_length=1)


class ProposedFailureSignature(Contract):
    code: str = Field(min_length=1)
    error_type: str | None = None
    message_pattern: str | None = None
    event_code: str | None = None


class DiagnosisProposal(Contract):
    schema_version: Literal["diagnosis-proposal/v1"] = "diagnosis-proposal/v1"
    symptom_summary: str = Field(min_length=1)
    affected_components: tuple[str, ...] = ()
    risk_tags: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    confirmed_facts: tuple[str, ...] = ()
    counterevidence: tuple[str, ...] = ()
    source_locations: tuple[ProposedSourceLocation, ...] = ()
    root_cause: str = Field(min_length=1)
    reproducer: Any = None
    original_input: Any = None
    failure_signature: ProposedFailureSignature
    missing_evidence: tuple[str, ...] = ()
    unresolved_unknowns: tuple[str, ...] = ()


__all__ = [
    "DiagnosisProposal",
    "ProposedFailureSignature",
    "ProposedSourceLocation",
]
