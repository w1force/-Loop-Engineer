"""Structured failure vocabulary and per-stage results.

This module fixes the core routing defect of the old ``VerificationCoordinator``:
its single ``except Exception`` funnelled *every* failure — infrastructure, policy,
integrity, observability — into the next repair round. Here, every failure carries
an explicit ``owner`` and only ``owner == REPAIR`` rejections consume a repair
cycle. The orchestrator's router (``core.orchestrator.router``) maps a
``FailureRecord`` to a ``NextAction``; contracts only define the vocabulary.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .base import Contract


class StageName(str, Enum):
    DIAGNOSIS = "diagnosis"
    REPAIR = "repair"
    VERIFICATION = "verification"
    RELEASE = "release"
    ORCHESTRATOR = "orchestrator"


class FailureOwner(str, Enum):
    """Who is responsible for a failure — determines the next action."""

    REPAIR = "repair"                # candidate code/behavior/regression is wrong
    DIAGNOSIS = "diagnosis"          # control cannot reproduce; root cause is wrong
    POLICY = "policy"                # plan violates policy / missing skill or proof
    INFRASTRUCTURE = "infrastructure"  # docker/network/registry/executor transient
    OBSERVABILITY = "observability"  # incomplete/late/ambiguous evidence
    INTEGRITY = "integrity"          # a frozen input changed after it was frozen
    RELEASE = "release"              # git/github side-effect failed


class Retryability(str, Enum):
    NONE = "none"
    SAME_STAGE = "same_stage"        # retry the same stage (infra/network)
    EXTERNAL_WAIT = "external_wait"  # wait for evidence/watermark then retry


class NextAction(str, Enum):
    RETRY_STAGE = "retry_stage"
    REDIAGNOSE = "rediagnose"
    REPLAN_SAME_CANDIDATE = "replan_same_candidate"
    REVERIFY_SAME_CANDIDATE = "reverify_same_candidate"
    NEW_REPAIR_ROUND = "new_repair_round"
    WAIT_EXTERNAL = "wait_external"
    INVALIDATE_RUN = "invalidate_run"
    ESCALATE = "escalate"


class StageStatus(str, Enum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"  # evidence rejected the candidate/behavior — a real code failure
    BLOCKED = "blocked"    # policy / missing skill / proof — NOT a code failure
    ERROR = "error"        # infra / provider / timeout — NOT a code failure


class FailureRecord(Contract):
    code: str = Field(min_length=1)
    stage: StageName
    owner: FailureOwner
    retryability: Retryability = Retryability.NONE
    consumes_repair_cycle: bool = False
    summary: str = Field(min_length=1)
    gate: str | None = None
    scenario_id: str | None = None
    candidate_digest: str | None = None
    evidence_refs: tuple[str, ...] = ()


class StageResult(Contract):
    """Uniform result every stage returns to the orchestrator.

    Output artifacts are passed structurally by the orchestrator (they are typed
    per stage); this envelope carries only status + structured failures so the
    router can decide the next action deterministically.
    """

    stage: StageName
    status: StageStatus
    failures: tuple[FailureRecord, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is StageStatus.SUCCEEDED and not self.failures


__all__ = [
    "FailureOwner",
    "FailureRecord",
    "NextAction",
    "Retryability",
    "StageName",
    "StageResult",
    "StageStatus",
]
