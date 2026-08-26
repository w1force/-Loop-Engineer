"""Cross-stage contracts for the Loop Engineer control plane.

Layering rule: stages and the orchestrator import artifact *shapes* from here;
trusted digests, machine gates and signed reports remain in ``core.verification``.
``failure`` and ``diagnosis`` are self-contained; ``incident`` and ``repair`` are
transitional facades over ``core.verification.workflow`` (see their TODOs).
"""

from __future__ import annotations

from .diagnosis import (
    DiagnosisProposal,
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
from .incident import (
    ArtifactReference,
    FailureSignature,
    IncidentBundle,
    SourceLocation,
)
from .repair import RepairCycleRequest, RepairFeedbackBundle, RepairResult

__all__ = [
    "ArtifactReference",
    "DiagnosisProposal",
    "FailureOwner",
    "FailureRecord",
    "FailureSignature",
    "IncidentBundle",
    "NextAction",
    "ProposedFailureSignature",
    "ProposedSourceLocation",
    "RepairCycleRequest",
    "RepairFeedbackBundle",
    "RepairResult",
    "Retryability",
    "SourceLocation",
    "StageName",
    "StageResult",
    "StageStatus",
]
