"""LoopEngineer orchestration layer.

Owns stage order, the run state machine, repair-round counting, structured failure
routing and escalation. It calls Diagnosis / Repair / Verification Control / Release
but implements none of their internals. This is the boundary the old
``VerificationCoordinator`` violated by owning repair + verify + release + retry.
"""

from __future__ import annotations

from .router import (
    FailureRouter,
    RoutingDecision,
    classify_exception,
    classify_replay_failures,
    classify_verification_report,
)

__all__ = [
    "FailureRouter",
    "RoutingDecision",
    "classify_exception",
    "classify_replay_failures",
    "classify_verification_report",
]
