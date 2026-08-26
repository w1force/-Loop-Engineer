"""LoopEngineer — the top-level orchestrator entry point.

Wires the full incident-repair loop the PRD describes:

    signal -> Diagnosis (read-only) -> IncidentFreezer -> IncidentBundle
           -> repair/verify loop (VerificationCoordinator, router-governed)
           -> Release (only on a signed VERIFIED report)

It adds the previously-missing Diagnosis stage in front of the proven
verification/repair loop and injects the new :class:`RepairStage` (frozen repair
SKILL, ordinary Coding Agent kernel) in place of the old FreshContextRepairAgent.
The orchestrator owns stage ordering and escalation; it never runs Bash, parses an
agent verdict, or forges a VerificationReport — those stay in Verification Control.
"""

from __future__ import annotations

from uuid import uuid4

from pydantic import Field

from core.contracts.base import Contract
from core.contracts.failure import FailureOwner, FailureRecord, StageName
from core.contracts.repair import (
    RepairCycleRequest,
    RepairFeedbackBundle,
    RepairResult,
)
from core.stages.diagnosis import DiagnosisRequest, DiagnosisStage, IncidentFreezer
from core.stages.repair import RepairStage
from core.verification.coordinator import (
    CoordinatorOutcome,
    CoordinatorRunRequest,
    CoordinatorStatus,
    VerificationCoordinator,
)
from core.verification.models import ARTICLE_MAX_VERIFICATION_ATTEMPTS


class LoopRunRequest(Contract):
    run_id: str = Field(
        default_factory=lambda: str(uuid4()),
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )
    diagnosis: DiagnosisRequest
    candidate_workspace: str = Field(min_length=1)
    max_cycles: int = Field(
        default=ARTICLE_MAX_VERIFICATION_ATTEMPTS,
        ge=1,
        le=ARTICLE_MAX_VERIFICATION_ATTEMPTS,
    )


class LoopOutcome(Contract):
    run_id: str
    incident_id: str
    incident_digest: str
    diagnosis_root_cause: str
    status: CoordinatorStatus
    cycle: int
    evidence_location: str | None = None
    release_reference: str | None = None
    escalation_reference: str | None = None
    failures: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.status in {CoordinatorStatus.VERIFIED, CoordinatorStatus.RELEASED}


class _RepairStageAgent:
    """Adapts :class:`RepairStage` to the coordinator's ``RepairAgent`` protocol.

    The coordinator hands ``previous_failures`` (already filtered to owner=REPAIR
    summaries by the router) inside the request; we re-wrap them as structured
    ``RepairFeedbackBundle`` findings for the repair SOP.
    """

    def __init__(self, *, stage: RepairStage, parent_agent_state, parent_params, tracer):
        self._stage = stage
        self._parent_agent_state = parent_agent_state
        self._parent_params = parent_params
        self._tracer = tracer

    async def repair(self, request: RepairCycleRequest) -> RepairResult:
        findings = tuple(
            FailureRecord(
                code="repair.feedback",
                stage=StageName.VERIFICATION,
                owner=FailureOwner.REPAIR,
                summary=summary,
                consumes_repair_cycle=True,
            )
            for summary in request.previous_failures
        )
        feedback = RepairFeedbackBundle(cycle=request.cycle, findings=findings)
        return await self._stage.repair(
            request,
            parent_agent_state=self._parent_agent_state,
            parent_params=self._parent_params,
            tracer=self._tracer,
            feedback=feedback,
        )


class LoopEngineer:
    """Owns stage order + escalation; delegates internals to the stages."""

    def __init__(
        self,
        *,
        diagnosis_stage: DiagnosisStage,
        incident_freezer: IncidentFreezer,
        repair_stage: RepairStage,
        coordinator: VerificationCoordinator,
    ):
        self.diagnosis_stage = diagnosis_stage
        self.incident_freezer = incident_freezer
        self.repair_stage = repair_stage
        self.coordinator = coordinator

    async def run(
        self,
        request: LoopRunRequest,
        *,
        parent_agent_state,
        parent_params,
        tracer,
        lightweight_verifier,
        planner,
        release_action=None,
        escalation_handler=None,
    ) -> LoopOutcome:
        request = LoopRunRequest.model_validate_json(request.model_dump_json())

        # 1. Diagnosis (read-only) -> untrusted proposal -> trusted IncidentBundle.
        proposal = await self.diagnosis_stage.run(
            request.diagnosis,
            parent_agent_state=parent_agent_state,
            parent_params=parent_params,
            tracer=tracer,
        )
        incident = self.incident_freezer.freeze(proposal, request=request.diagnosis)

        # 2. Repair/verify loop, governed by the coordinator's FailureRouter.
        coord_request = CoordinatorRunRequest(
            run_id=request.run_id,
            incident=incident,
            control_workspace=request.diagnosis.control_workspace,
            candidate_workspace=request.candidate_workspace,
            max_cycles=request.max_cycles,
        )
        repair_agent = _RepairStageAgent(
            stage=self.repair_stage,
            parent_agent_state=parent_agent_state,
            parent_params=parent_params,
            tracer=tracer,
        )
        outcome: CoordinatorOutcome = await self.coordinator.run(
            coord_request,
            repair_agent=repair_agent,
            lightweight_verifier=lightweight_verifier,
            planner=planner,
            release_action=release_action,
            escalation_handler=escalation_handler,
        )

        return LoopOutcome(
            run_id=outcome.run_id,
            incident_id=incident.incident_id,
            incident_digest=incident.digest,
            diagnosis_root_cause=proposal.root_cause,
            status=outcome.status,
            cycle=outcome.cycle,
            evidence_location=outcome.evidence_location,
            release_reference=outcome.release_reference,
            escalation_reference=outcome.escalation_reference,
            failures=outcome.failures,
        )


__all__ = [
    "LoopEngineer",
    "LoopOutcome",
    "LoopRunRequest",
]
