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
    "LoopEngineer",
    "LoopOutcome",
    "LoopRunRequest",
]


def __getattr__(name: str):
    # Lazy re-export of the heavy orchestrator to avoid importing the verification
    # coordinator (and its deps) just to use the router.
    if name in {"LoopEngineer", "LoopOutcome", "LoopRunRequest"}:
        from . import loop_engineer

        return getattr(loop_engineer, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
