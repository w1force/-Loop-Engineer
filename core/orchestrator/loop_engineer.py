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
from core.contracts.diagnosis import DiagnosisProposal
from core.contracts.evidence import ReproductionAssessment, ReproductionDisposition
from core.contracts.failure import FailureOwner, FailureRecord, StageName
from core.contracts.repair import (
    RepairCycleRequest,
    RepairFeedbackBundle,
    RepairResult,
)
from core.stages.diagnosis import (
    CommandControlReproducer,
    ControlReproducer,
    DiagnosisReproductionRequest,
    DiagnosisRequest,
    DiagnosisRetryAction,
    DiagnosisRetryController,
    DiagnosisRetryError,
    DiagnosisStage,
    IncidentFreezer,
)
from core.stages.repair import RepairStage
from core.verification.coordinator import (
    CoordinatorOutcome,
    CoordinatorRunRequest,
    CoordinatorStatus,
    HumanEscalation,
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
    max_diagnosis_attempts: int = Field(
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
    reproduction_disposition: ReproductionDisposition | None = None
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
        diagnosis_retry_controller: DiagnosisRetryController | None = None,
        control_reproducer: ControlReproducer | None = None,
        learning_sink=None,
    ):
        self.diagnosis_stage = diagnosis_stage
        self.incident_freezer = incident_freezer
        self.repair_stage = repair_stage
        self.coordinator = coordinator
        self.diagnosis_retry_controller = (
            diagnosis_retry_controller or DiagnosisRetryController()
        )
        self.control_reproducer = control_reproducer or CommandControlReproducer(
            evidence_store=self.diagnosis_retry_controller.state_store
        )
        self.learning_sink = learning_sink

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
        learning_sink=None,
        escalation_handler=None,
        control_reproducer: ControlReproducer | None = None,
    ) -> LoopOutcome:
        request = LoopRunRequest.model_validate_json(request.model_dump_json())
        active_learning_sink = learning_sink
        if active_learning_sink is None:
            active_learning_sink = self.learning_sink
        if active_learning_sink is None and release_action is not None:
            provider = getattr(parent_params, "provider", None)
            if provider is not None:
                from core.learning.runtime import build_default_learning_service

                active_learning_sink = build_default_learning_service(
                    provider,
                    agent_model=parent_params.model,
                    tracer=tracer.child(stage="repair_learning"),
                )
        state_store = self.diagnosis_retry_controller.state_store
        active_reproducer = control_reproducer or self.control_reproducer

        # 1. Optional source-backed evidence expansion, then bounded Diagnosis /
        #    control-reproduction attempts. Only distinct hypotheses consume the
        #    three-attempt diagnosis budget.
        previous_attempts: tuple[ReproductionAssessment, ...] = ()
        first_attempt = 1
        if state_store is not None:
            previous_attempts = tuple(
                ReproductionAssessment(
                    attempt=int(item["attempt"]),
                    disposition=ReproductionDisposition(item["disposition"]),
                    summary=item["summary"],
                    evidence_refs=item["evidence_refs"],
                    hypothesis_digest=item["hypothesis_digest"],
                )
                for item in state_store.list_diagnosis_attempts(
                    request.diagnosis.incident_id
                )
            )
            first_attempt = len(previous_attempts) + 1
        attempt_limit = request.max_diagnosis_attempts
        if first_attempt > attempt_limit:
            raise DiagnosisRetryError("diagnosis attempt budget is already exhausted")
        if state_store is not None:
            state_store.mark_incident_signals(
                request.diagnosis.incident_id, processing_state="diagnosing"
            )
        proposal = None
        incident = None
        reproduction_ready = False
        last_assessment: ReproductionAssessment | None = None
        diagnosis_request = request.diagnosis
        for diagnosis_attempt in range(first_attempt, attempt_limit + 1):
            diagnosis_request = request.diagnosis.model_copy(
                update={"previous_attempts": previous_attempts}
            )
            prepare_request = getattr(self.diagnosis_stage, "prepare_request", None)
            if callable(prepare_request):
                diagnosis_request = await self.diagnosis_stage.prepare_request(
                    diagnosis_request,
                    parent_agent_state=parent_agent_state,
                    parent_params=parent_params,
                    tracer=tracer,
                )
            proposal = await self.diagnosis_stage.run(
                diagnosis_request,
                parent_agent_state=parent_agent_state,
                parent_params=parent_params,
                tracer=tracer,
                attempt=diagnosis_attempt,
            )
            incident = self.incident_freezer.freeze(
                proposal, request=diagnosis_request
            )
            assessment = await active_reproducer.assess(
                DiagnosisReproductionRequest(
                    incident=incident,
                    proposal=proposal,
                    attempt=diagnosis_attempt,
                    control_workspace=diagnosis_request.control_workspace,
                    previous_attempts=previous_attempts,
                )
            )
            assessment = ReproductionAssessment.model_validate_json(
                assessment.model_dump_json()
            )
            last_assessment = assessment
            try:
                decision = self.diagnosis_retry_controller.record_and_decide(
                    incident_id=incident.incident_id,
                    proposal=proposal,
                    assessment=assessment,
                )
            except DiagnosisRetryError as exc:
                return await self._diagnosis_escalation(
                    request=request,
                    incident=incident,
                    proposal=proposal,
                    attempt=diagnosis_attempt,
                    failures=(str(exc),),
                    escalation_handler=escalation_handler,
                    state_store=state_store,
                )
            previous_attempts = (*previous_attempts, assessment)
            if decision.action is DiagnosisRetryAction.PROCEED:
                reproduction_ready = True
                break
            if decision.action is DiagnosisRetryAction.REDIAGNOSE:
                continue
            if decision.action is DiagnosisRetryAction.TERMINATE:
                if state_store is not None:
                    stale = assessment.disposition in {
                        ReproductionDisposition.STALE_SIGNAL,
                        ReproductionDisposition.OLD_VERSION_SIGNAL,
                    }
                    state_store.close_incident(
                        incident.incident_id,
                        status="stale" if stale else "duplicate",
                        processing_state="stale" if stale else "ignored",
                    )
                return LoopOutcome(
                    run_id=request.run_id,
                    incident_id=incident.incident_id,
                    incident_digest=incident.digest,
                    diagnosis_root_cause=proposal.root_cause,
                    status=CoordinatorStatus.NO_ACTION,
                    cycle=diagnosis_attempt,
                    reproduction_disposition=assessment.disposition,
                    failures=(assessment.summary,),
                )
            return await self._diagnosis_escalation(
                request=request,
                incident=incident,
                proposal=proposal,
                attempt=diagnosis_attempt,
                failures=(assessment.summary,),
                escalation_handler=escalation_handler,
                disposition=assessment.disposition,
                state_store=state_store,
            )

        assert proposal is not None and incident is not None
        if not reproduction_ready:
            assert last_assessment is not None
            return await self._diagnosis_escalation(
                request=request,
                incident=incident,
                proposal=proposal,
                attempt=last_assessment.attempt,
                failures=(
                    "diagnosis reproduction attempt budget exhausted: "
                    + last_assessment.summary,
                ),
                escalation_handler=escalation_handler,
                disposition=last_assessment.disposition,
                state_store=state_store,
            )
        assert last_assessment is not None

        # 2. Repair/verify loop, governed by the coordinator's FailureRouter.
        coord_request = CoordinatorRunRequest(
            run_id=request.run_id,
            incident=incident,
            control_workspace=diagnosis_request.control_workspace,
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
            learning_sink=active_learning_sink,
            escalation_handler=escalation_handler,
        )

        if state_store is not None and outcome.status in {
            CoordinatorStatus.VERIFIED,
            CoordinatorStatus.RELEASED,
        }:
            assert outcome.candidate_ref is not None
            state_store.record_resolution(
                incident_id=incident.incident_id,
                verified_candidate=outcome.candidate_ref,
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
            reproduction_disposition=last_assessment.disposition,
            failures=outcome.failures,
        )

    @staticmethod
    async def _diagnosis_escalation(
        *,
        request: LoopRunRequest,
        incident,
        proposal: DiagnosisProposal,
        attempt: int,
        failures: tuple[str, ...],
        escalation_handler,
        disposition: ReproductionDisposition | None = None,
        state_store=None,
    ) -> LoopOutcome:
        if state_store is not None:
            state_store.close_incident(
                incident.incident_id,
                status="escalated",
                processing_state="escalated",
            )
        escalation_reference = None
        if escalation_handler is not None:
            escalation_reference = await escalation_handler.escalate(
                HumanEscalation(
                    run_id=request.run_id,
                    incident_id=incident.incident_id,
                    exhausted_cycles=attempt,
                    failures=failures,
                )
            )
        return LoopOutcome(
            run_id=request.run_id,
            incident_id=incident.incident_id,
            incident_digest=incident.digest,
            diagnosis_root_cause=proposal.root_cause,
            status=CoordinatorStatus.ESCALATED,
            cycle=attempt,
            escalation_reference=escalation_reference,
            reproduction_disposition=disposition,
            failures=failures,
        )


__all__ = [
    "LoopEngineer",
    "LoopOutcome",
    "LoopRunRequest",
]
