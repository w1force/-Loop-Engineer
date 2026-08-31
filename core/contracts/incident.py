"""Incident contracts — the frozen, trustworthy diagnosis output later stages read.

``IncidentBundle`` and its trusted sub-models currently live in
``core.verification.workflow``; this module is the canonical incident import surface
and re-exports them so diagnosis/repair/orchestrator code imports from
``core.contracts`` rather than the verification namespace.

TODO(arch): physically relocate these definitions here once the verification
namespace split is complete.
"""

from __future__ import annotations

from core.verification.workflow import (
    ArtifactReference,
    FailureSignature,
    IncidentBundle,
    SourceLocation,
)

__all__ = [
    "ArtifactReference",
    "FailureSignature",
    "IncidentBundle",
    "SourceLocation",
]
