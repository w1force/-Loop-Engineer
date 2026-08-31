"""Cross-stage contracts for the Loop Engineer control plane.

Layering rule: stages and the orchestrator import artifact *shapes* from here;
trusted digests, machine gates and signed reports remain in ``core.verification``.
``failure`` and ``diagnosis`` are self-contained; ``incident`` and ``repair`` are
transitional facades over ``core.verification.workflow`` (see their TODOs).
"""

from __future__ import annotations

from .diagnosis import (
    DiagnosisProposal,
    DiagnosisReproducerSpec,
    ProposedFailureSignature,
    ProposedSourceLocation,
)
from .failure import (
    FailureOwner,
    FailureRecord,
    NextAction,
    Retryability,
    StageName,
    StageResult,
    StageStatus,
)
from .evidence import (
    CorrelationIdentity,
    CorrelationProof,
    DiagnosisEvidenceBundle,
    EvidenceLogPlan,
    EvidenceLogPlanProposal,
    EvidenceMatchStatus,
    EvidenceObservation,
    EvidenceResult,
    ExpectedPresence,
    FrozenEvidenceLog,
    IncidentSignal,
    PrimarySignal,
    ProposedEvidenceLog,
    ReproductionAssessment,
    ReproductionDisposition,
    SignalRole,
    TemporalRelation,
)
from .incident import (
    ArtifactReference,
    FailureSignature,
    IncidentBundle,
    SourceLocation,
)
from .repair import RepairCycleRequest, RepairFeedbackBundle, RepairResult

__all__ = [
    "ArtifactReference",
    "CorrelationIdentity",
    "CorrelationProof",
    "DiagnosisProposal",
    "DiagnosisReproducerSpec",
    "DiagnosisEvidenceBundle",
    "EvidenceLogPlan",
    "EvidenceLogPlanProposal",
    "EvidenceMatchStatus",
    "EvidenceObservation",
    "EvidenceResult",
    "ExpectedPresence",
    "FailureOwner",
    "FailureRecord",
    "FailureSignature",
    "FrozenEvidenceLog",
    "IncidentSignal",
    "IncidentBundle",
    "NextAction",
    "PrimarySignal",
    "ProposedEvidenceLog",
    "ProposedFailureSignature",
    "ProposedSourceLocation",
    "RepairCycleRequest",
    "RepairFeedbackBundle",
    "RepairResult",
    "Retryability",
    "ReproductionAssessment",
    "ReproductionDisposition",
    "SignalRole",
    "SourceLocation",
    "StageName",
    "StageResult",
    "StageStatus",
    "TemporalRelation",
]
