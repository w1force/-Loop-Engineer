"""Discovery contracts: detection outcome and the signal envelope."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field

from core.contracts.base import Contract


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Eligibility(str, Enum):
    """The four-box test result (PRD §4): what the loop is allowed to do."""

    AUTO_FIX_ELIGIBLE = "auto_fix_eligible"
    DIAGNOSE_ONLY = "diagnose_only"
    RECORD_ONLY = "record_only"


class Detection(Contract):
    matched_rule: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    severity: Severity
    eligibility: Eligibility
    error_type: str | None = None
    message: str = Field(default="")


class SignalEnvelope(Contract):
    """Deduped, deterministically-detected signal — the input to the loop."""

    schema_version: str = "signal-envelope/v1"
    signal_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    service: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    observed_at: str = Field(min_length=1)
    matched_rule: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    severity: Severity
    eligibility: Eligibility
    error_type: str | None = None
    message: str = ""
    original_input: Any = None
    evidence: dict[str, Any] = Field(default_factory=dict)


__all__ = ["Detection", "Eligibility", "Severity", "SignalEnvelope"]
