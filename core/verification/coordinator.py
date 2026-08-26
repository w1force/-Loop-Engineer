"""Trusted orchestration for repair -> replay -> hard verification -> release.

Agents may propose repairs and plans, but this module owns ordering, immutable
snapshots, retry limits, signed evidence persistence, and the release transition.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from enum import Enum
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Protocol
from uuid import uuid4

from pydantic import Field, StrictInt, field_validator, model_validator

from .engine import VerificationEngine
from .models import (
    ARTICLE_MAX_VERIFICATION_ATTEMPTS,
    VerificationModel,
    VerificationReport,
    ReplayEvidenceManifest,
    VerificationVerdict,
)
from .runner import workspace_digest
from .replay import DockerReplayLauncher
from .store import AttestedJsonEvidenceStore
from .workflow import (
    CandidateSnapshot,
    IncidentBundle,
    LightweightVerificationRequest,
    LightweightVerificationResult,
    LightweightVerdict,
    RepairCycleRequest,
    RepairResult,
    VerificationPlan,
    VerificationPlanFreezer,
    VerificationPlanProposal,
    VerificationPlanningRequest,
    canonical_json_digest,
    capture_candidate_snapshot,
)


class RepairAgent(Protocol):
    async def repair(self, request: RepairCycleRequest) -> RepairResult: ...


class LightweightVerifier(Protocol):
    async def verify(
        self, request: LightweightVerificationRequest
    ) -> LightweightVerificationResult: ...


class VerificationPlanner(Protocol):
    async def propose(
        self, request: VerificationPlanningRequest
    ) -> VerificationPlanProposal: ...


class ReplayReceiptLike(Protocol):
    run_id: str
    cycle: int
    plan_digest: str
    control_digest: str
    candidate_ref: str
    candidate_digest: str
    policy_digest: str
    skill_digests: dict[str, str]
    passed: bool
    failures: tuple[str, ...]
    replay_manifest: ReplayEvidenceManifest

    @property
    def digest(self) -> str: ...

    def model_dump(self, *, mode: str) -> dict[str, Any]: ...


class ReplayLauncher(Protocol):
    async def replay(self, plan: VerificationPlan) -> ReplayReceiptLike: ...


class VerifiedReleaseAction(Protocol):
    async def release_verified(self, request: "VerifiedReleaseRequest") -> str: ...


class EscalationHandler(Protocol):
    async def escalate(self, request: "HumanEscalation") -> str | None: ...


class CoordinatorStatus(str, Enum):
    RUNNING = "running"
    VERIFIED = "verified"
    RELEASED = "released"
    RELEASE_BLOCKED = "release_blocked"
    ESCALATED = "escalated"


class CycleStage(str, Enum):
    REPAIR = "repair"
    LIGHTWEIGHT_VERIFICATION = "lightweight_verification"
    PLAN_FROZEN = "plan_frozen"
    REPLAY = "replay"
    HARD_VERIFICATION = "hard_verification"
    COMPLETE = "complete"


class CoordinatorRunRequest(VerificationModel):
    run_id: str = Field(
        default_factory=lambda: str(uuid4()),
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )
    incident: IncidentBundle
    control_workspace: str = Field(min_length=1)
    candidate_workspace: str = Field(min_length=1)
    max_cycles: StrictInt = Field(
        default=ARTICLE_MAX_VERIFICATION_ATTEMPTS,
        ge=1,
        le=ARTICLE_MAX_VERIFICATION_ATTEMPTS,
    )

    @model_validator(mode="after")
    def _separate_workspaces(self):
        if Path(self.control_workspace).resolve() == Path(
            self.candidate_workspace
        ).resolve():
            raise ValueError("control and candidate workspaces must be distinct")
        return self


class CycleRecord(VerificationModel):
    cycle: StrictInt = Field(ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    stage: CycleStage
    candidate_ref: str | None = None
    candidate_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    lightweight_verdict: LightweightVerdict | None = None
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    plan_path: str | None = None
    replay_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    replay_path: str | None = None
    report_verdict: VerificationVerdict | None = None
    evidence_location: str | None = None
    failures: tuple[str, ...] = ()


class CoordinatorState(VerificationModel):
    schema_version: str = Field(pattern=r"^verification-coordinator-state/v1$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    incident_id: str
    incident_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_ref: str = Field(min_length=1)
    control_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: CoordinatorStatus
    cycles: tuple[CycleRecord, ...] = ()
    release_reference: str | None = None
    escalation_reference: str | None = None

    @model_validator(mode="after")
    def _ordered_cycles(self):
        actual = [item.cycle for item in self.cycles]
        if actual != list(range(1, len(actual) + 1)):
            raise ValueError("coordinator cycles must be contiguous from one")
        return self


class CoordinatorOutcome(VerificationModel):
    run_id: str
    incident_id: str
    status: CoordinatorStatus
    cycle: StrictInt = Field(ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    evidence_location: str | None = None
    release_reference: str | None = None
    escalation_reference: str | None = None
    failures: tuple[str, ...] = ()


class VerifiedReleaseRequest(VerificationModel):
    """Only constructed after a VERIFIED report has been signed and persisted."""

    run_id: str
    cycle: StrictInt = Field(ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    incident_id: str
    incident_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_manifest: ReplayEvidenceManifest
    scenario_input_digests: dict[str, str] = Field(min_length=1)
    candidate_ref: str
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_location: str = Field(min_length=1)

    @field_validator("scenario_input_digests")
    @classmethod
    def _valid_scenario_input_digests(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not scenario_id
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            for scenario_id, digest in value.items()
        ):
            raise ValueError("scenario_input_digests must contain SHA-256 values")
        return value


class HumanEscalation(VerificationModel):
    run_id: str
    incident_id: str
    exhausted_cycles: StrictInt = Field(ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    failures: tuple[str, ...] = Field(min_length=1)


class CoordinatorStateStore:
    """Durable run state plus write-once plans and replay receipts."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def _run_dir(self, run_id: str) -> Path:
        if not run_id or any(char in run_id for char in ("/", "\\", "\x00")):
            raise ValueError("unsafe run_id")
        target = (self.root / run_id).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("coordinator state path escapes root") from exc
        return target

    @contextmanager
    def lock(self, run_id: str):
        directory = self._run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / ".lock"
        with lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f"coordinator run is already active: {run_id}") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def initialize(self, state: CoordinatorState) -> None:
        state = CoordinatorState.model_validate_json(state.model_dump_json())
        target = self._run_dir(state.run_id) / "state.json"
        if target.exists():
            raise FileExistsError(f"coordinator run already exists: {state.run_id}")
        self._atomic_write(target, self._model_bytes(state), replace=False)

    def save(self, state: CoordinatorState) -> None:
        state = CoordinatorState.model_validate_json(state.model_dump_json())
        target = self._run_dir(state.run_id) / "state.json"
        if not target.is_file():
            raise FileNotFoundError("coordinator state was not initialized")
        existing = CoordinatorState.model_validate_json(target.read_bytes())
        if (
            existing.run_id != state.run_id
            or existing.incident_id != state.incident_id
            or existing.incident_digest != state.incident_digest
            or existing.control_ref != state.control_ref
            or existing.control_digest != state.control_digest
        ):
            raise ValueError("immutable coordinator identity changed")
        self._atomic_write(target, self._model_bytes(state), replace=True)

    def freeze_plan(self, plan: VerificationPlan) -> str:
        target = self._run_dir(plan.run_id) / f"cycle-{plan.cycle}" / "plan.json"
        payload = self._model_bytes(plan)
        if target.exists():
            existing = VerificationPlan.model_validate_json(target.read_bytes())
            if existing.digest != plan.digest:
                raise FileExistsError("a different verification plan is already frozen")
            return str(target)
        self._atomic_write(target, payload, replace=False)
        self.load_plan(plan.run_id, plan.cycle, expected_digest=plan.digest)
        return str(target)

    def load_plan(
        self, run_id: str, cycle: int, *, expected_digest: str
    ) -> VerificationPlan:
        target = self._run_dir(run_id) / f"cycle-{cycle}" / "plan.json"
        plan = VerificationPlan.model_validate_json(target.read_bytes())
        if plan.digest != expected_digest:
            raise ValueError("frozen verification plan digest changed")
        return plan

    def persist_replay_receipt(
        self,
        *,
        run_id: str,
        cycle: int,
        receipt: ReplayReceiptLike,
    ) -> str:
        target = self._run_dir(run_id) / f"cycle-{cycle}" / "replay.json"
        payload = (
            json.dumps(
                receipt.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        if target.exists():
            if canonical_json_digest(json.loads(target.read_text("utf-8"))) != canonical_json_digest(
                json.loads(payload)
            ):
                raise FileExistsError("a different replay receipt already exists")
            return str(target)
        self._atomic_write(target, payload, replace=False)
        return str(target)

    @staticmethod
    def _model_bytes(model: VerificationModel) -> bytes:
        return (
            json.dumps(
                model.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _atomic_write(target: Path, payload: bytes, *, replace: bool) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not replace and target.exists():
            raise FileExistsError(str(target))
        fd, temporary_name = tempfile.mkstemp(
            prefix=".tmp-", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if not replace and target.exists():
                raise FileExistsError(str(target))
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def _report_failures(report: VerificationReport) -> tuple[str, ...]:
    failures: list[str] = []
    for result in report.gate_results:
        failures.extend(f"{result.gate.value}: {item}" for item in result.failures)
    if not failures:
        failures.append(f"hard verification verdict: {report.verdict.value}")
    return tuple(failures)


def _validate_report_binding(
    report: VerificationReport, plan: VerificationPlan, receipt: ReplayReceiptLike
) -> None:
    expected = {
        "run_id": plan.run_id,
        "cycle": plan.cycle,
        "incident_id": plan.incident_id,
        "incident_digest": plan.incident_digest,
        "plan_digest": plan.digest,
        "replay_digest": receipt.digest,
        "replay_manifest": receipt.replay_manifest,
        "scenario_input_digests": plan.scenario_input_digests,
        "control_ref": plan.control_ref,
        "control_digest": plan.control_digest,
        "candidate_ref": plan.candidate_ref,
        "candidate_digest": plan.candidate_digest,
        "policy_digest": plan.policy_digest,
        "skill_names": plan.skill_names,
        "skill_digests": plan.skill_digests,
    }
    mismatches = [
        name for name, value in expected.items() if getattr(report, name) != value
    ]
    if mismatches:
        raise ValueError(
            "verification report does not match frozen plan: " + ", ".join(mismatches)
        )


def _validate_replay_binding(
    receipt: ReplayReceiptLike, plan: VerificationPlan
) -> None:
    expected = {
        "run_id": plan.run_id,
        "cycle": plan.cycle,
        "plan_digest": plan.digest,
        "control_digest": plan.control_digest,
        "candidate_ref": plan.candidate_ref,
        "candidate_digest": plan.candidate_digest,
        "policy_digest": plan.policy_digest,
        "skill_digests": plan.skill_digests,
    }
    mismatches = [
        name for name, value in expected.items() if getattr(receipt, name) != value
    ]
    if mismatches:
        raise ValueError(
            "replay receipt does not match frozen plan: " + ", ".join(mismatches)
        )


class VerificationCoordinator:
    """The sole state owner and release gate for an incident repair loop."""

    def __init__(
        self,
        *,
        plan_freezer: VerificationPlanFreezer,
        engine: VerificationEngine,
        replay_launcher: ReplayLauncher,
        evidence_store: AttestedJsonEvidenceStore,
        state_store: CoordinatorStateStore,
    ):
        if type(engine) is not VerificationEngine:
            raise TypeError("Coordinator only accepts the built-in VerificationEngine")
        if type(evidence_store) is not AttestedJsonEvidenceStore:
            raise TypeError("Coordinator requires AttestedJsonEvidenceStore")
        if type(replay_launcher) is not DockerReplayLauncher:
            raise TypeError("Coordinator only accepts the built-in DockerReplayLauncher")
        if engine.policy.digest != plan_freezer.policy.digest:
            raise ValueError("engine and plan freezer policy differ")
        self.plan_freezer = plan_freezer
        self.engine = engine
        self.replay_launcher = replay_launcher
        self.evidence_store = evidence_store
        self.state_store = state_store

    async def run(
        self,
        request: CoordinatorRunRequest,
        *,
        repair_agent: RepairAgent,
        lightweight_verifier: LightweightVerifier,
        planner: VerificationPlanner,
        release_action: VerifiedReleaseAction | None = None,
        escalation_handler: EscalationHandler | None = None,
    ) -> CoordinatorOutcome:
        request = CoordinatorRunRequest.model_validate_json(request.model_dump_json())
        with self.state_store.lock(request.run_id):
            return await self._run_locked(
                request,
                repair_agent=repair_agent,
                lightweight_verifier=lightweight_verifier,
                planner=planner,
                release_action=release_action,
                escalation_handler=escalation_handler,
            )

    async def _run_locked(
        self,
        request: CoordinatorRunRequest,
        *,
        repair_agent: RepairAgent,
        lightweight_verifier: LightweightVerifier,
        planner: VerificationPlanner,
        release_action: VerifiedReleaseAction | None,
        escalation_handler: EscalationHandler | None,
    ) -> CoordinatorOutcome:
        # Lazy import breaks the module-load cycle
        # (orchestrator.router -> verification.models -> verification.__init__ ->
        #  coordinator). These are only needed at run time.
        from core.contracts.failure import StageName
        from core.orchestrator.router import (
            FailureRouter,
            classify_exception,
            classify_replay_failures,
            classify_verification_report,
        )

        control_workspace = Path(request.control_workspace).resolve()
        candidate_workspace = Path(request.candidate_workspace).resolve()
        control_digest = await asyncio.to_thread(
            workspace_digest,
            control_workspace,
            self.plan_freezer.policy.workspace_ignore,
        )
        state = CoordinatorState(
            schema_version="verification-coordinator-state/v1",
            run_id=request.run_id,
            incident_id=request.incident.incident_id,
            incident_digest=request.incident.digest,
            control_ref=request.incident.control_ref,
            control_digest=control_digest,
            status=CoordinatorStatus.RUNNING,
        )
        self.state_store.initialize(state)
        router = FailureRouter(max_repair_rounds=request.max_cycles)
        prior_failures: tuple[str, ...] = ()
        latest_evidence: str | None = None

        for cycle in range(1, request.max_cycles + 1):
            record = CycleRecord(cycle=cycle, stage=CycleStage.REPAIR)
            state = state.model_copy(update={"cycles": (*state.cycles, record)})
            self.state_store.save(state)
            try:
                repair = await repair_agent.repair(
                    RepairCycleRequest(
                        run_id=request.run_id,
                        cycle=cycle,
                        incident=request.incident,
                        candidate_workspace=str(candidate_workspace),
                        previous_failures=prior_failures,
                    )
                )
                repair = RepairResult.model_validate_json(repair.model_dump_json())
                if Path(repair.workspace).resolve() != candidate_workspace:
                    raise ValueError("Repair Agent returned an untrusted workspace")
                candidate = await asyncio.to_thread(
                    capture_candidate_snapshot,
                    control_workspace=control_workspace,
                    repair=repair,
                    workspace_ignore=self.plan_freezer.policy.workspace_ignore,
                )
                self._assert_control_unchanged(control_workspace, control_digest)

                light = await lightweight_verifier.verify(
                    LightweightVerificationRequest(
                        run_id=request.run_id,
                        cycle=cycle,
                        incident=request.incident,
                        candidate=candidate,
                    )
                )
                light = LightweightVerificationResult.model_validate_json(
                    light.model_dump_json()
                )
                record = record.model_copy(
                    update={
                        "stage": CycleStage.LIGHTWEIGHT_VERIFICATION,
                        "candidate_ref": candidate.candidate_ref,
                        "candidate_digest": candidate.candidate_digest,
                        "lightweight_verdict": light.verdict,
                    }
                )
                state = self._replace_cycle(state, record)
                self.state_store.save(state)
                if light.verdict is not LightweightVerdict.PASS:
                    raise RuntimeError(
                        "lightweight verification blocked the next stage: "
                        + light.report
                    )

                proposal = await planner.propose(
                    VerificationPlanningRequest(
                        run_id=request.run_id,
                        cycle=cycle,
                        incident=request.incident,
                        control_workspace=str(control_workspace),
                        candidate=candidate,
                        policy=self.plan_freezer.policy,
                        policy_digest=self.plan_freezer.policy.digest,
                        available_skills=self.plan_freezer.available_skills(
                            request.incident.matched_rule
                        ),
                        available_generation_skills=(
                            self.plan_freezer.available_generation_skills()
                        ),
                    )
                )
                plan = self.plan_freezer.freeze(
                    proposal,
                    run_id=request.run_id,
                    cycle=cycle,
                    incident=request.incident,
                    candidate=candidate,
                    control_digest=control_digest,
                )
                plan_path = self.state_store.freeze_plan(plan)
                self._assert_plan_and_sources_unchanged(
                    plan,
                    control_workspace=control_workspace,
                )
                record = record.model_copy(
                    update={
                        "stage": CycleStage.PLAN_FROZEN,
                        "plan_digest": plan.digest,
                        "plan_path": plan_path,
                    }
                )
                state = self._replace_cycle(state, record)
                self.state_store.save(state)

                receipt = await self.replay_launcher.replay(plan)
                _validate_replay_binding(receipt, plan)
                replay_path = self.state_store.persist_replay_receipt(
                    run_id=request.run_id,
                    cycle=cycle,
                    receipt=receipt,
                )
                self._assert_plan_and_sources_unchanged(
                    plan,
                    control_workspace=control_workspace,
                )
                record = record.model_copy(
                    update={
                        "stage": CycleStage.REPLAY,
                        "replay_digest": receipt.digest,
                        "replay_path": replay_path,
                    }
                )
                state = self._replace_cycle(state, record)
                self.state_store.save(state)
                if not receipt.passed:
                    replay_findings = classify_replay_failures(
                        receipt.failures, candidate_digest=candidate.candidate_digest
                    )
                    state, prior_failures, outcome = await self._route_and_record(
                        findings=replay_findings,
                        state=state,
                        cycle=cycle,
                        router=router,
                        request=request,
                        escalation_handler=escalation_handler,
                        latest_evidence=latest_evidence,
                    )
                    if outcome is not None:
                        return outcome
                    continue

                report = await self.engine.verify(
                    plan.to_run_request(replay_receipt=receipt)
                )
                report = VerificationReport.model_validate_json(
                    report.model_dump_json()
                )
                _validate_report_binding(report, plan, receipt)
                self._assert_plan_and_sources_unchanged(
                    plan,
                    control_workspace=control_workspace,
                )
                latest_evidence = await self.evidence_store.persist(report)
                record = record.model_copy(
                    update={
                        "stage": CycleStage.COMPLETE,
                        "report_verdict": report.verdict,
                        "evidence_location": latest_evidence,
                        "failures": (
                            ()
                            if report.verdict is VerificationVerdict.VERIFIED
                            else _report_failures(report)
                        ),
                    }
                )
                state = self._replace_cycle(state, record)
                self.state_store.save(state)

                if report.verdict is not VerificationVerdict.VERIFIED:
                    report_findings = classify_verification_report(report)
                    state, prior_failures, outcome = await self._route_and_record(
                        findings=report_findings,
                        state=state,
                        cycle=cycle,
                        router=router,
                        request=request,
                        escalation_handler=escalation_handler,
                        latest_evidence=latest_evidence,
                    )
                    if outcome is not None:
                        return outcome
                    continue

                state = state.model_copy(update={"status": CoordinatorStatus.VERIFIED})
                self.state_store.save(state)
                if release_action is None:
                    return CoordinatorOutcome(
                        run_id=request.run_id,
                        incident_id=request.incident.incident_id,
                        status=CoordinatorStatus.VERIFIED,
                        cycle=cycle,
                        evidence_location=latest_evidence,
                    )
                try:
                    release_reference = await release_action.release_verified(
                        VerifiedReleaseRequest(
                            run_id=request.run_id,
                            cycle=cycle,
                            incident_id=request.incident.incident_id,
                            incident_digest=request.incident.digest,
                            plan_digest=plan.digest,
                            replay_digest=receipt.digest,
                            replay_manifest=receipt.replay_manifest,
                            scenario_input_digests=plan.scenario_input_digests,
                            candidate_ref=plan.candidate_ref,
                            candidate_digest=plan.candidate_digest,
                            evidence_location=latest_evidence,
                        )
                    )
                except Exception as exc:
                    failure = f"release blocked: {type(exc).__name__}: {exc}"
                    state = state.model_copy(
                        update={"status": CoordinatorStatus.RELEASE_BLOCKED}
                    )
                    self.state_store.save(state)
                    return CoordinatorOutcome(
                        run_id=request.run_id,
                        incident_id=request.incident.incident_id,
                        status=CoordinatorStatus.RELEASE_BLOCKED,
                        cycle=cycle,
                        evidence_location=latest_evidence,
                        failures=(failure,),
                    )
                state = state.model_copy(
                    update={
                        "status": CoordinatorStatus.RELEASED,
                        "release_reference": release_reference,
                    }
                )
                self.state_store.save(state)
                return CoordinatorOutcome(
                    run_id=request.run_id,
                    incident_id=request.incident.incident_id,
                    status=CoordinatorStatus.RELEASED,
                    cycle=cycle,
                    evidence_location=latest_evidence,
                    release_reference=release_reference,
                )
            except Exception as exc:
                exc_findings = (classify_exception(exc, stage=StageName.VERIFICATION),)
                state, prior_failures, outcome = await self._route_and_record(
                    findings=exc_findings,
                    state=state,
                    cycle=cycle,
                    router=router,
                    request=request,
                    escalation_handler=escalation_handler,
                    latest_evidence=latest_evidence,
                )
                if outcome is not None:
                    return outcome

        return await self._escalate(
            state=state,
            request=request,
            failures=prior_failures or ("verification attempts exhausted",),
            escalation_handler=escalation_handler,
            cycle=request.max_cycles,
            latest_evidence=latest_evidence,
        )

    async def _route_and_record(
        self,
        *,
        findings,
        state: CoordinatorState,
        cycle: int,
        router: FailureRouter,
        request: CoordinatorRunRequest,
        escalation_handler: "EscalationHandler | None",
        latest_evidence: str | None,
    ) -> tuple[CoordinatorState, tuple[str, ...], CoordinatorOutcome | None]:
        """Route a cycle's structured failures.

        Only an ``owner == REPAIR`` governing failure feeds the next repair round;
        every other owner (infrastructure/policy/integrity/observability/diagnosis)
        is terminal for this run and escalates immediately, instead of silently
        consuming repair rounds as the old catch-all did. Returns
        ``(state, repair_feedback, outcome)`` — a non-None outcome means return it,
        otherwise ``continue`` with ``repair_feedback`` as the next round's input.
        """

        from core.contracts.failure import FailureOwner, NextAction

        decision = router.route(findings, repair_rounds_used=cycle - 1)
        summaries = tuple(f.summary for f in findings) or ("unspecified failure",)
        current = state.cycles[-1].model_copy(
            update={"stage": CycleStage.COMPLETE, "failures": summaries}
        )
        state = self._replace_cycle(state, current)
        self.state_store.save(state)
        if decision.next_action is NextAction.NEW_REPAIR_ROUND:
            repair_feedback = (
                tuple(f.summary for f in findings if f.owner is FailureOwner.REPAIR)
                or summaries
            )
            return state, repair_feedback, None
        outcome = await self._escalate(
            state=state,
            request=request,
            failures=(
                f"[{decision.owner.value} -> {decision.next_action.value}] "
                + decision.reason,
            )
            + summaries,
            escalation_handler=escalation_handler,
            cycle=cycle,
            latest_evidence=latest_evidence,
        )
        return state, (), outcome

    async def _escalate(
        self,
        *,
        state: CoordinatorState,
        request: CoordinatorRunRequest,
        failures: tuple[str, ...],
        escalation_handler: "EscalationHandler | None",
        cycle: int,
        latest_evidence: str | None,
    ) -> CoordinatorOutcome:
        escalation = HumanEscalation(
            run_id=request.run_id,
            incident_id=request.incident.incident_id,
            exhausted_cycles=request.max_cycles,
            failures=failures,
        )
        escalation_reference = None
        if escalation_handler is not None:
            escalation_reference = await escalation_handler.escalate(escalation)
        state = state.model_copy(
            update={
                "status": CoordinatorStatus.ESCALATED,
                "escalation_reference": escalation_reference,
            }
        )
        self.state_store.save(state)
        return CoordinatorOutcome(
            run_id=request.run_id,
            incident_id=request.incident.incident_id,
            status=CoordinatorStatus.ESCALATED,
            cycle=cycle,
            evidence_location=latest_evidence,
            escalation_reference=escalation_reference,
            failures=escalation.failures,
        )

    @staticmethod
    def _replace_cycle(
        state: CoordinatorState, record: CycleRecord
    ) -> CoordinatorState:
        if not state.cycles or state.cycles[-1].cycle != record.cycle:
            raise ValueError("can only update the active coordinator cycle")
        return state.model_copy(update={"cycles": (*state.cycles[:-1], record)})

    def _assert_control_unchanged(self, workspace: Path, expected: str) -> None:
        actual = workspace_digest(
            workspace, self.plan_freezer.policy.workspace_ignore
        )
        if actual != expected:
            raise RuntimeError("control workspace changed during the coordinator run")

    def _assert_plan_and_sources_unchanged(
        self,
        plan: VerificationPlan,
        *,
        control_workspace: Path,
    ) -> None:
        self.state_store.load_plan(
            plan.run_id, plan.cycle, expected_digest=plan.digest
        )
        self._assert_control_unchanged(control_workspace, plan.control_digest)
        candidate = workspace_digest(
            plan.workspace, self.plan_freezer.policy.workspace_ignore
        )
        if candidate != plan.candidate_digest:
            raise RuntimeError("candidate changed after VerificationPlan freeze")


__all__ = [
    "CoordinatorOutcome",
    "CoordinatorRunRequest",
    "CoordinatorState",
    "CoordinatorStateStore",
    "CoordinatorStatus",
    "CycleRecord",
    "CycleStage",
    "EscalationHandler",
    "HumanEscalation",
    "LightweightVerifier",
    "RepairAgent",
    "ReplayLauncher",
    "VerificationCoordinator",
    "VerificationPlanner",
    "VerifiedReleaseAction",
    "VerifiedReleaseRequest",
]
