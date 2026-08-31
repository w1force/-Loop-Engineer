"""Discovery: versioned-rule detection + fingerprint dedup over ingested logs.

The detector is a deterministic control band (PRD FR-AUTO-003): a versioned rule set
decides whether a log record is an actionable signal and how eligible it is for auto
repair — an LLM never makes that call. Matching signals are fingerprinted and deduped
into incidents (FR-AUTO-004), which the bridge turns into a DiagnosisRequest for the
LoopEngineer.
"""

from __future__ import annotations

from .bridge import incident_to_diagnosis_request
from .contracts import Detection, Eligibility, Severity, SignalEnvelope
from .detector import DEFAULT_RULESET, DetectionRule, RuleSet
from .fingerprint import compute_fingerprint, normalize_message
from .pipeline import DiscoveryPipeline, DiscoveryResult

__all__ = [
    "DEFAULT_RULESET",
    "Detection",
    "DetectionRule",
    "DiscoveryPipeline",
    "DiscoveryResult",
    "Eligibility",
    "RuleSet",
    "Severity",
    "SignalEnvelope",
    "compute_fingerprint",
    "incident_to_diagnosis_request",
    "normalize_message",
]
