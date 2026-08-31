from __future__ import annotations

from pathlib import Path
import sys

import pytest
import yaml
from pydantic import StrictBool

from core.verification.coordinator import (
    CoordinatorRunRequest,
    CoordinatorStateStore,
    CoordinatorStatus,
    VerificationCoordinator,
)
from core.verification.engine import VerificationEngine
from core.verification.replay import DockerReplayLauncher
from core.verification.models import (
    BehaviorEvidence,
    BehaviorGateSpec,
    BehaviorObservation,
    BehaviorScenarioSpec,
    CommandSpec,
    LintGateSpec,
    LogEvidence,
    LogGateSpec,
    ReplayEvidenceManifest,
    ReplayWindowBinding,
    ScenarioCollection,
    TraceEvidence,
    TraceGateSpec,
    TraceObservation,
    ToolCallObservation,
    UIGateSpec,
    UnitGateSpec,
    Variant,
    VerificationModel,
    VerificationPolicy,
    VerificationVerdict,
)
from core.verification.skill import VerificationSkillLoader
from core.verification.store import AttestedJsonEvidenceStore
from core.verification.workflow import (
    ArtifactReference,
    FailureSignature,
    IncidentBundle,
    LightweightVerificationResult,
    LightweightVerdict,
    RepairResult,
    ReproductionSpec,
    SourceLocation,
    VerificationPlanFreezer,
    VerificationPlanProposal,
    canonical_json_digest,
)


def _artifact(name: str) -> ArtifactReference:
    return ArtifactReference(uri=f"file:///evidence/{name}", sha256="a" * 64)


def _incident() -> IncidentBundle:
    return IncidentBundle(
        incident_id="incident-1",
        requirement="The checkout request must recover from the diagnosed timeout.",
        matched_rule="checkout-timeout",
        error_logs=(_artifact("error.jsonl"),),
        original_trace=_artifact("trace.json"),
        source_locations=(
            SourceLocation(
                path="service.py", start_line=1, revision="control-sha"
            ),
        ),
        root_cause="The request did not use the fallback path.",
        control_ref="control-sha",
        original_input={"prompt": "checkout"},
        failure_signature=FailureSignature(
            code="checkout.timeout",
            error_type="TimeoutError",
        ),
    )


def _workspaces(tmp_path: Path) -> tuple[Path, Path]:
    control = tmp_path / "control"
    candidate = tmp_path / "candidate"
    control.mkdir()
    candidate.mkdir()
    (control / "service.py").write_text("result = 'timeout'\n", encoding="utf-8")
    (candidate / "service.py").write_text("result = 'timeout'\n", encoding="utf-8")
    return control, candidate


def _skill_loader(tmp_path: Path) -> VerificationSkillLoader:
    root = tmp_path / "skills"
    skill = root / "checkout"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: checkout\ndescription: focused checkout verification\n---\n"
        "Verify only the frozen checkout scenario.\n",
        encoding="utf-8",
    )
    (skill / "verification.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "checkout",
                "version": "1",
                "description": "focused checkout verification",
                "integration": [
                    {
                        "id": "case",
                        "description": "checkout reproducer",
                        "steps": [
                            {
                                "id": "focused",
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    "print('focused ok')",
                                ],
                                "stdout_contains": ["focused ok"],
                            }
                        ],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return VerificationSkillLoader([root])


def _policy() -> VerificationPolicy:
    return VerificationPolicy(
        lint=LintGateSpec(
            checks=(
                CommandSpec(
                    id="lint",
                    argv=(sys.executable, "-c", "print('clean')"),
                ),
            )
        ),
        unit=UnitGateSpec(
            checks=(
                CommandSpec(
                    id="unit",
                    argv=(sys.executable, "-c", "print('unit ok')"),
                ),
            )
        ),
        trace=TraceGateSpec(expected_model="model-a"),
        staging_log=LogGateSpec(),
        behavior=BehaviorGateSpec(
            scenarios=(
                BehaviorScenarioSpec(
                    scenario_id="checkout:case",
                    expected_control_outcome="failure",
                    allowed_changed_paths=("$.status",),
                    required_changed_paths=("$.status",),
                    forbidden_changed_paths=("@model", "@tool_calls"),
                    reproducer=True,
                ),
            )
        ),
        ui=UIGateSpec(
            mode="not_applicable", not_applicable_reason="API-only test service"
        ),
    )


def _proposal(incident: IncidentBundle) -> VerificationPlanProposal:
    return VerificationPlanProposal(
        skill_names=("checkout",),
        reproductions=(
            ReproductionSpec(
                scenario_id="checkout:case",
                skill_name="checkout",
                input_payload=incident.original_input,
                input_digest=canonical_json_digest(incident.original_input),
                reproducer=True,
                failure_signature=incident.failure_signature,
                expected_control_outcome="failure",
                expected_candidate_outcome="success",
                allowed_changed_paths=("$.status",),
                required_changed_paths=("$.status",),
                forbidden_changed_paths=("@model", "@tool_calls"),
                regression_assertions=("focused",),
                boundary_assertions=("focused",),
                side_effect_assertions=("focused",),
            ),
        ),
    )


class _Repair:
    def __init__(self, candidate: Path):
        self.candidate = candidate
        self.calls = []

    async def repair(self, request):
        self.calls.append(request)
        (self.candidate / "service.py").write_text(
            f"result = 'fixed-{request.cycle}'\n", encoding="utf-8"
        )
        return RepairResult(
            workspace=str(self.candidate),
            candidate_ref=f"candidate-{request.cycle}",
            implementation_summary="Use the fallback path.",
            test_entrypoints=("pytest tests/test_checkout.py",),
        )


class _Lightweight:
    def __init__(self, verdicts: tuple[LightweightVerdict, ...]):
        self.verdicts = list(verdicts)
        self.calls = []

    async def verify(self, request):
        self.calls.append(request)
        verdict = self.verdicts.pop(0)
        return LightweightVerificationResult(
            verdict=verdict,
            report=f"candidate-focused result\nVERDICT: {verdict.value.upper()}",
        )


class _Planner:
    def __init__(self, proposal: VerificationPlanProposal):
        self.proposal = proposal
        self.calls = []

    async def propose(self, request):
        self.calls.append(request)
        return self.proposal


class _ReplayReceipt(VerificationModel):
    run_id: str
    cycle: int
    plan_digest: str
    control_digest: str
    candidate_ref: str
    candidate_digest: str
    policy_digest: str
    skill_digests: dict[str, str]
    passed: StrictBool
    failures: tuple[str, ...] = ()
    replay_manifest: ReplayEvidenceManifest

    @property
    def digest(self) -> str:
        return canonical_json_digest(self.model_dump(mode="json"))


class _Replay:
    def __init__(self, state_store: CoordinatorStateStore, *, passed: bool = True):
        self.state_store = state_store
        self.passed = passed
        self.calls = []

    async def replay(self, plan):
        # A plan must already be durable before any candidate result is observed.
        frozen = self.state_store.load_plan(
            plan.run_id, plan.cycle, expected_digest=plan.digest
        )
        assert frozen.reproductions == plan.reproductions
        self.calls.append(plan)
        return _ReplayReceipt(
            run_id=plan.run_id,
            cycle=plan.cycle,
            plan_digest=plan.digest,
            control_digest=plan.control_digest,
            candidate_ref=plan.candidate_ref,
            candidate_digest=plan.candidate_digest,
            policy_digest=plan.policy_digest,
            skill_digests=plan.skill_digests,
            passed=self.passed,
            failures=(() if self.passed else ("control did not reproduce",)),
            replay_manifest=ReplayEvidenceManifest(
                windows=tuple(
                    ReplayWindowBinding(
                        scenario_id=reproduction.scenario_id,
                        variant=variant,
                        input_digest=reproduction.input_digest,
                        collection_id=(
                            f"{plan.run_id}-{plan.cycle}-"
                            f"{reproduction.scenario_id}-{variant.value}"
                        ),
                        otlp_barrier_digest="4" * 64,
                        oracle_digest="5" * 64,
                        result_sha256="6" * 64,
                    )
                    for reproduction in plan.reproductions
                    for variant in (Variant.CONTROL, Variant.CANDIDATE)
                )
            ),
        )


def _trusted_replay(fake: _Replay) -> DockerReplayLauncher:
    launcher = object.__new__(DockerReplayLauncher)
    launcher.replay = fake.replay  # type: ignore[method-assign]
    return launcher


class _TraceProvider:
    def __init__(self, input_digest: str):
        self.input_digest = input_digest

    async def collect_trace(self, context):
        return TraceEvidence(
            run_id=context.run_id,
            cycle=context.cycle,
            candidate_ref=context.candidate_ref,
            candidate_digest=context.candidate_digest,
            policy_digest=context.policy_digest,
            skill_digests=context.skill_digests,
            collection_complete=True,
            observations=(
                TraceObservation(
                    trace_id="1" * 32,
                    request_id="candidate-request",
                    scenario_id="checkout:case",
                    input_digest=self.input_digest,
                    error_observations=0,
                    actual_model="model-a",
                    fallback_used=False,
                    input_tokens=42,
                    finished=True,
                ),
            ),
        )


class _LogProvider:
    def __init__(self, input_digest: str):
        self.input_digest = input_digest

    async def collect_logs(self, context):
        return LogEvidence(
            run_id=context.run_id,
            cycle=context.cycle,
            control_ref=context.control_ref,
            control_digest=context.control_digest,
            candidate_ref=context.candidate_ref,
            candidate_digest=context.candidate_digest,
            policy_digest=context.policy_digest,
            skill_digests=context.skill_digests,
            collection_complete=True,
            collected_variants=(Variant.CONTROL, Variant.CANDIDATE),
            collected_scenarios=tuple(
                ScenarioCollection(
                    scenario_id="checkout:case",
                    variant=variant,
                    collection_id=f"logs-{variant.value}",
                    input_digest=self.input_digest,
                )
                for variant in (Variant.CONTROL, Variant.CANDIDATE)
            ),
        )


class _BehaviorProvider:
    def __init__(self, input_digest: str):
        self.input_digest = input_digest

    async def collect_behavior(self, context):
        tool = ToolCallObservation(
            sequence=0,
            tool_name="checkout",
            input_digest="1" * 64,
            output_digest="2" * 64,
            outcome="success",
        )
        return BehaviorEvidence(
            run_id=context.run_id,
            cycle=context.cycle,
            control_ref=context.control_ref,
            control_digest=context.control_digest,
            candidate_ref=context.candidate_ref,
            candidate_digest=context.candidate_digest,
            policy_digest=context.policy_digest,
            skill_digests=context.skill_digests,
            collection_complete=True,
            collected_variants=(Variant.CONTROL, Variant.CANDIDATE),
            collected_scenarios=tuple(
                ScenarioCollection(
                    scenario_id="checkout:case",
                    variant=variant,
                    collection_id=f"behavior-{variant.value}",
                    input_digest=self.input_digest,
                )
                for variant in (Variant.CONTROL, Variant.CANDIDATE)
            ),
            observations=(
                BehaviorObservation(
                    scenario_id="checkout:case",
                    variant=Variant.CONTROL,
                    outcome="failure",
                    payload={"status": "timeout"},
                    input_digest=self.input_digest,
                    model="model-a",
                    tool_calls=(tool,),
                    finished=True,
                    request_id="control-request",
                    trace_id="0" * 32,
                ),
                BehaviorObservation(
                    scenario_id="checkout:case",
                    variant=Variant.CANDIDATE,
                    outcome="success",
                    payload={"status": "ok"},
                    input_digest=self.input_digest,
                    model="model-a",
                    tool_calls=(tool,),
                    finished=True,
                    request_id="candidate-request",
                    trace_id="1" * 32,
                ),
            ),
        )


class _Escalation:
    def __init__(self):
        self.calls = []

    async def escalate(self, request):
        self.calls.append(request)
        return "ticket://incident-1"


class _Release:
    def __init__(self):
        self.calls = []

    async def release_verified(self, request):
        self.calls.append(request)
        evidence = Path(request.evidence_location)
        assert (evidence / "report.json").is_file()
        assert (evidence / "attestation.json").is_file()
        return "https://github.com/example/repo/pull/1"


def _coordinator(tmp_path: Path):
    policy = _policy()
    loader = _skill_loader(tmp_path)
    input_digest = canonical_json_digest(_incident().original_input)
    engine = VerificationEngine(
        policy=policy,
        skill_loader=loader,
        trace_provider=_TraceProvider(input_digest),
        log_provider=_LogProvider(input_digest),
        behavior_provider=_BehaviorProvider(input_digest),
    )
    state_store = CoordinatorStateStore(tmp_path / "state")
    coordinator = VerificationCoordinator(
        plan_freezer=VerificationPlanFreezer(
            policy=policy,
            skill_loader=loader,
            allowed_skill_names=("checkout",),
        ),
        engine=engine,
        replay_launcher=_trusted_replay(_Replay(state_store)),
        evidence_store=AttestedJsonEvidenceStore(
            tmp_path / "evidence",
            signing_key=b"s" * 32,
            app_id="test-app",
            repository="example/repo",
        ),
        state_store=state_store,
    )
    return coordinator, state_store


@pytest.mark.asyncio
async def test_three_lightweight_failures_escalate_without_plan_or_release(
    tmp_path: Path,
) -> None:
    control, candidate = _workspaces(tmp_path)
    coordinator, state_store = _coordinator(tmp_path)
    repair = _Repair(candidate)
    light = _Lightweight(
        (LightweightVerdict.FAIL,) * 3
    )
    planner = _Planner(_proposal(_incident()))
    escalation = _Escalation()
    release = _Release()

    outcome = await coordinator.run(
        CoordinatorRunRequest(
            run_id="run-three-failures",
            incident=_incident(),
            control_workspace=str(control),
            candidate_workspace=str(candidate),
        ),
        repair_agent=repair,
        lightweight_verifier=light,
        planner=planner,
        release_action=release,
        escalation_handler=escalation,
    )

    assert outcome.status is CoordinatorStatus.ESCALATED
    assert len(repair.calls) == 3
    assert repair.calls[1].previous_failures
    assert not planner.calls
    assert not release.calls
    assert len(escalation.calls) == 1
    state = state_store._run_dir(outcome.run_id) / "state.json"
    assert '"status": "escalated"' in state.read_text("utf-8")


@pytest.mark.asyncio
async def test_replay_failure_is_a_hard_block_and_never_calls_engine_or_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, candidate = _workspaces(tmp_path)
    coordinator, state_store = _coordinator(tmp_path)
    replay = _Replay(state_store, passed=False)
    coordinator.replay_launcher = _trusted_replay(replay)
    engine_calls = 0

    async def forbidden_verify(_request):
        nonlocal engine_calls
        engine_calls += 1
        raise AssertionError("engine must not run after rejected replay")

    monkeypatch.setattr(coordinator.engine, "verify", forbidden_verify)
    release = _Release()
    outcome = await coordinator.run(
        CoordinatorRunRequest(
            run_id="run-replay-failure",
            incident=_incident(),
            control_workspace=str(control),
            candidate_workspace=str(candidate),
            max_cycles=1,
        ),
        repair_agent=_Repair(candidate),
        lightweight_verifier=_Lightweight((LightweightVerdict.PASS,)),
        planner=_Planner(_proposal(_incident())),
        release_action=release,
    )

    assert outcome.status is CoordinatorStatus.REDIAGNOSIS_REQUIRED
    assert outcome.failure_owner == "diagnosis"
    assert outcome.next_action == "rediagnose"
    assert len(replay.calls) == 1
    assert engine_calls == 0
    assert not release.calls
    assert "control did not reproduce" in outcome.failures[0]


@pytest.mark.asyncio
async def test_only_signed_verified_report_can_reach_release(tmp_path: Path) -> None:
    control, candidate = _workspaces(tmp_path)
    coordinator, _ = _coordinator(tmp_path)
    release = _Release()

    outcome = await coordinator.run(
        CoordinatorRunRequest(
            run_id="run-verified",
            incident=_incident(),
            control_workspace=str(control),
            candidate_workspace=str(candidate),
            max_cycles=1,
        ),
        repair_agent=_Repair(candidate),
        lightweight_verifier=_Lightweight((LightweightVerdict.PASS,)),
        planner=_Planner(_proposal(_incident())),
        release_action=release,
    )

    assert outcome.status is CoordinatorStatus.RELEASED
    assert len(release.calls) == 1
    assert outcome.evidence_location
    report = Path(outcome.evidence_location) / "report.json"
    assert f'"verdict": "{VerificationVerdict.VERIFIED.value}"' in report.read_text(
        "utf-8"
    )


@pytest.mark.asyncio
async def test_candidate_change_after_plan_freeze_invalidates_cycle(
    tmp_path: Path,
) -> None:
    control, candidate = _workspaces(tmp_path)
    coordinator, state_store = _coordinator(tmp_path)

    class MutatingReplay(_Replay):
        async def replay(self, plan):
            receipt = await super().replay(plan)
            (Path(plan.workspace) / "service.py").write_text(
                "tampered after freeze\n", encoding="utf-8"
            )
            return receipt

    coordinator.replay_launcher = _trusted_replay(MutatingReplay(state_store))
    release = _Release()
    outcome = await coordinator.run(
        CoordinatorRunRequest(
            run_id="run-stale-candidate",
            incident=_incident(),
            control_workspace=str(control),
            candidate_workspace=str(candidate),
            max_cycles=1,
        ),
        repair_agent=_Repair(candidate),
        lightweight_verifier=_Lightweight((LightweightVerdict.PASS,)),
        planner=_Planner(_proposal(_incident())),
        release_action=release,
    )

    assert outcome.status is CoordinatorStatus.ESCALATED
    assert "candidate changed after VerificationPlan freeze" in outcome.failures[0]
    assert not release.calls
