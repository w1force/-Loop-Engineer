from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest
import yaml
from pydantic import ValidationError

from core.verification import (
    AttestedJsonEvidenceStore,
    BehaviorEvidence,
    BehaviorGateSpec,
    BehaviorObservation,
    BehaviorScenarioSpec,
    CommandSpec,
    GateKind,
    GateResult,
    GateStatus,
    JsonEvidenceStore,
    LintGateSpec,
    LogEvidence,
    LogGateSpec,
    LogObservation,
    ScenarioCollection,
    ScenarioSpec,
    TraceEvidence,
    TraceGateSpec,
    TraceObservation,
    ToolCallObservation,
    UIGateSpec,
    UnitGateSpec,
    Variant,
    VerificationEngine,
    VerificationPolicy,
    VerificationRunRequest,
    VerificationReport,
    VerificationSkillLoader,
    VerificationVerdict,
    workspace_digest,
)


TOOL_CALL = ToolCallObservation(
    sequence=0,
    tool_name="checkout",
    input_digest="1" * 64,
    output_digest="2" * 64,
    outcome="success",
)


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.txt").write_text("candidate\n", encoding="utf-8")
    return workspace


def _make_skill(tmp_path: Path) -> VerificationSkillLoader:
    root = tmp_path / "skills"
    directory = root / "checkout"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: checkout\ndescription: checkout flow\n---\nVerify checkout.\n",
        encoding="utf-8",
    )
    spec = {
        "name": "checkout",
        "version": "1",
        "description": "checkout flow",
        "integration": [
            {
                "id": "case",
                "description": "focused checkout",
                "steps": [
                    {
                        "id": "integration",
                        "argv": [sys.executable, "-c", "print('integration ok')"],
                        "stdout_contains": ["integration ok"],
                    }
                ],
            }
        ],
    }
    (directory / "verification.yaml").write_text(
        yaml.safe_dump(spec, sort_keys=False), encoding="utf-8"
    )
    return VerificationSkillLoader([root])


def _policy(
    *,
    lint_code: str = "print('clean')",
    ui: str = "not_applicable",
    evidence_timeout_ms: int = 120_000,
) -> VerificationPolicy:
    return VerificationPolicy(
        lint=LintGateSpec(
            checks=(
                CommandSpec(id="lint", argv=(sys.executable, "-c", lint_code)),
            )
        ),
        unit=UnitGateSpec(
            checks=(
                CommandSpec(id="unit", argv=(sys.executable, "-c", "print('all tests')")),
            )
        ),
        trace=TraceGateSpec(expected_model="model-a"),
        staging_log=LogGateSpec(),
        behavior=BehaviorGateSpec(
            scenarios=(
                BehaviorScenarioSpec(
                    scenario_id="checkout:case",
                    expected_control_outcome="failure",
                    expected_candidate_outcome="success",
                    allowed_changed_paths=("$.status", "$.body.ok"),
                    reproducer=True,
                ),
            )
        ),
        ui=UIGateSpec(
            mode=ui,
            not_applicable_reason=("API-only service" if ui == "not_applicable" else None),
        ),
        evidence_timeout_ms=evidence_timeout_ms,
    )


class GoodTraceProvider:
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
                    trace_id="trace-candidate",
                    request_id="request-candidate",
                    scenario_id="checkout:case",
                    input_digest="a" * 64,
                    error_observations=0,
                    actual_model="model-a",
                    fallback_used=False,
                    input_tokens=42,
                    finished=True,
                ),
            ),
        )


class GoodLogProvider:
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
                    scenario_id=scenario_id,
                    variant=variant,
                    collection_id=f"logs-{variant.value}-{scenario_id}",
                    input_digest="a" * 64,
                )
                for scenario_id in context.scenario_ids
                for variant in (Variant.CONTROL, Variant.CANDIDATE)
            ),
        )


class GoodBehaviorProvider:
    async def collect_behavior(self, context):
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
            collected_scenarios=(
                ScenarioCollection(
                    scenario_id="checkout:case",
                    variant=Variant.CONTROL,
                    collection_id="behavior-control-checkout",
                    input_digest="a" * 64,
                ),
                ScenarioCollection(
                    scenario_id="checkout:case",
                    variant=Variant.CANDIDATE,
                    collection_id="behavior-candidate-checkout",
                    input_digest="a" * 64,
                ),
            ),
            observations=(
                BehaviorObservation(
                    scenario_id="checkout:case",
                    variant=Variant.CONTROL,
                    outcome="failure",
                    payload={"status": 500, "body": {"ok": False}},
                    input_digest="a" * 64,
                    model="model-a",
                    tool_calls=(TOOL_CALL,),
                    finished=True,
                    request_id="old-request",
                    trace_id="old-trace",
                ),
                BehaviorObservation(
                    scenario_id="checkout:case",
                    variant=Variant.CANDIDATE,
                    outcome="success",
                    payload={"status": 200, "body": {"ok": True}},
                    input_digest="a" * 64,
                    model="model-a",
                    tool_calls=(TOOL_CALL,),
                    finished=True,
                    request_id="request-candidate",
                    trace_id="trace-candidate",
                ),
            ),
        )


def _request(
    workspace: Path,
    engine: VerificationEngine,
    skill: str = "checkout",
) -> VerificationRunRequest:
    digest = (
        engine.skill_loader.load(skill).digest
        if skill == "checkout"
        else "d" * 64
    )
    return VerificationRunRequest(
        run_id="run-1",
        workspace=str(workspace),
        control_ref="control-sha",
        control_digest="e" * 64,
        candidate_ref="candidate-sha",
        expected_candidate_digest=workspace_digest(
            workspace, engine.policy.workspace_ignore
        ),
        expected_policy_digest=engine.policy.digest,
        skill_names=(skill,),
        expected_skill_digests={skill: digest},
    )


def _engine(tmp_path: Path, **overrides) -> VerificationEngine:
    values = {
        "policy": _policy(),
        "skill_loader": _make_skill(tmp_path),
        "trace_provider": GoodTraceProvider(),
        "log_provider": GoodLogProvider(),
        "behavior_provider": GoodBehaviorProvider(),
    }
    values.update(overrides)
    return VerificationEngine(**values)


def test_engine_rejects_untrusted_command_runner(tmp_path: Path) -> None:
    class FakeRunner:
        async def run(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("untrusted runner was called")

    with pytest.raises(TypeError, match="信任边界"):
        VerificationEngine(
            policy=_policy(),
            skill_loader=_make_skill(tmp_path),
            command_runner=FakeRunner(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_all_article_gates_can_produce_verified_report(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    engine = _engine(tmp_path)
    report = await engine.verify(_request(workspace, engine))

    assert report.verdict is VerificationVerdict.VERIFIED
    assert {result.gate for result in report.gate_results} == set(GateKind)
    assert next(
        item for item in report.gate_results if item.gate is GateKind.UI
    ).status is GateStatus.NOT_APPLICABLE
    assert report.candidate_digest == report.candidate_digest_after
    assert report.skill_digests.keys() == {"checkout"}


@pytest.mark.asyncio
async def test_required_ui_gate_can_produce_verified_report(tmp_path: Path) -> None:
    class UITraceProvider:
        async def collect_trace(self, context):
            observations = []
            for scenario_id in context.scenario_ids:
                is_behavior = scenario_id == "checkout:case"
                observations.append(
                    TraceObservation(
                        trace_id="trace-candidate" if is_behavior else "trace-ui",
                        request_id=(
                            "request-candidate" if is_behavior else "request-ui"
                        ),
                        scenario_id=scenario_id,
                        input_digest="a" * 64,
                        error_observations=0,
                        actual_model="model-a",
                        fallback_used=False,
                        input_tokens=42,
                        finished=True,
                    )
                )
            return TraceEvidence(
                run_id=context.run_id,
                cycle=context.cycle,
                candidate_ref=context.candidate_ref,
                candidate_digest=context.candidate_digest,
                policy_digest=context.policy_digest,
                skill_digests=context.skill_digests,
                collection_complete=True,
                observations=tuple(observations),
            )

    ui = UIGateSpec(
        mode="required",
        global_scenarios=(
            ScenarioSpec(
                id="page",
                description="render page",
                steps=(
                    CommandSpec(
                        id="render",
                        argv=(sys.executable, "-c", "print('page ok')"),
                        stdout_contains=("page ok",),
                    ),
                ),
            ),
        ),
    )
    policy = _policy().model_copy(update={"ui": ui})
    workspace = _make_workspace(tmp_path)
    engine = VerificationEngine(
        policy=policy,
        skill_loader=_make_skill(tmp_path),
        trace_provider=UITraceProvider(),
        log_provider=GoodLogProvider(),
        behavior_provider=GoodBehaviorProvider(),
    )

    report = await engine.verify(_request(workspace, engine))
    restored = VerificationReport.model_validate_json(report.model_dump_json())

    assert restored.verdict is VerificationVerdict.VERIFIED
    assert all(item.status is GateStatus.PASS for item in restored.gate_results)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    ["trace", "log", "behavior", "command_contract", "stdout_hash", "cycle"],
)
async def test_verified_report_recomputes_raw_evidence(
    tmp_path: Path, tamper: str
) -> None:
    workspace = _make_workspace(tmp_path)
    engine = _engine(tmp_path)
    report = await engine.verify(_request(workspace, engine))
    payload = report.model_dump(mode="python")

    if tamper == "trace":
        payload["trace_evidence"]["observations"][0]["actual_model"] = "wrong"
    elif tamper == "log":
        added = LogObservation(
            observation_id="new-log-error",
            scenario_id="checkout:case",
            variant=Variant.CANDIDATE,
            service="api",
            level="ERROR",
            error_type="DatabaseError",
            request_id="request-candidate",
            trace_id="trace-candidate",
        ).model_dump(mode="python")
        payload["log_evidence"]["observations"] = [added]
        for gate in payload["gate_results"]:
            if gate["gate"] is GateKind.STAGING_LOG:
                gate["evidence_ids"] = ["new-log-error"]
    elif tamper == "behavior":
        payload["behavior_evidence"]["observations"][1]["payload"]["total"] = 1
    elif tamper == "command_contract":
        payload["command_evidence"][0]["command_spec_digest"] = "f" * 64
    elif tamper == "stdout_hash":
        payload["command_evidence"][0]["stdout_sha256"] = "f" * 64
    else:
        payload["cycle"] = 2

    with pytest.raises(ValidationError):
        VerificationReport.model_validate(payload)


@pytest.mark.asyncio
async def test_candidate_digest_is_frozen_before_verification(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    engine = _engine(tmp_path)
    request = _request(workspace, engine)
    (workspace / "app.txt").write_text("changed after freeze\n", encoding="utf-8")

    report = await engine.verify(request)

    assert report.verdict is VerificationVerdict.BLOCKED
    assert all(item.status is GateStatus.BLOCKED for item in report.gate_results)


@pytest.mark.asyncio
async def test_missing_external_providers_block_instead_of_passing(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    engine = VerificationEngine(
        policy=_policy(),
        skill_loader=_make_skill(tmp_path),
    )
    report = await engine.verify(_request(workspace, engine))

    assert report.verdict is VerificationVerdict.BLOCKED
    by_gate = {item.gate: item.status for item in report.gate_results}
    assert by_gate[GateKind.TRACE] is GateStatus.BLOCKED
    assert by_gate[GateKind.STAGING_LOG] is GateStatus.BLOCKED
    assert by_gate[GateKind.BEHAVIOR_COMPARE] is GateStatus.BLOCKED


@pytest.mark.asyncio
async def test_unknown_skill_and_required_ui_without_scenario_are_blocked(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    engine = _engine(tmp_path, policy=_policy(ui="required"))
    report = await engine.verify(
        _request(workspace, engine, skill="missing")
    )
    by_gate = {item.gate: item for item in report.gate_results}
    assert by_gate[GateKind.INTEGRATION].status is GateStatus.BLOCKED
    assert by_gate[GateKind.UI].status is GateStatus.BLOCKED
    assert report.verdict is VerificationVerdict.BLOCKED


@pytest.mark.asyncio
async def test_ui_not_applicable_conflicts_with_selected_skill_ui_scenario(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    engine = _engine(tmp_path)
    config_path = tmp_path / "skills" / "checkout" / "verification.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["ui"] = [
        {
            "id": "checkout-page",
            "description": "render checkout",
            "steps": [{"id": "ui", "argv": [sys.executable, "-c", "print('ui')"]}],
        }
    ]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    report = await engine.verify(_request(workspace, engine))
    ui = next(item for item in report.gate_results if item.gate is GateKind.UI)

    assert ui.status is GateStatus.BLOCKED
    assert report.verdict is VerificationVerdict.BLOCKED


@pytest.mark.asyncio
async def test_skill_digest_must_be_frozen_before_repair(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    engine = _engine(tmp_path)
    request = _request(workspace, engine)
    skill_file = tmp_path / "skills" / "checkout" / "SKILL.md"
    skill_file.write_text(skill_file.read_text(encoding="utf-8") + "changed\n")

    report = await engine.verify(request)
    integration = next(
        item for item in report.gate_results if item.gate is GateKind.INTEGRATION
    )
    assert integration.status is GateStatus.BLOCKED
    assert "冻结摘要不一致" in integration.summary


@pytest.mark.asyncio
async def test_lint_warning_is_a_hard_failure(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    engine = _engine(
        tmp_path,
        policy=_policy(lint_code="print('warning: unused import')"),
    )
    report = await engine.verify(_request(workspace, engine))
    lint = next(item for item in report.gate_results if item.gate is GateKind.LINT)
    assert lint.status is GateStatus.FAIL
    assert report.verdict is VerificationVerdict.REJECTED


@pytest.mark.asyncio
async def test_external_provider_timeout_fails_closed(tmp_path: Path) -> None:
    class SlowTraceProvider:
        async def collect_trace(self, context):
            del context
            await asyncio.sleep(1)
            raise AssertionError("provider timeout was not enforced")

    workspace = _make_workspace(tmp_path)
    engine = _engine(
        tmp_path,
        policy=_policy(evidence_timeout_ms=100),
        trace_provider=SlowTraceProvider(),
    )

    report = await engine.verify(_request(workspace, engine))
    trace = next(item for item in report.gate_results if item.gate is GateKind.TRACE)

    assert trace.status is GateStatus.ERROR
    assert report.verdict is VerificationVerdict.ERROR


@pytest.mark.asyncio
async def test_provider_cannot_turn_timeout_into_pass_by_swallowing_cancel(
    tmp_path: Path,
) -> None:
    class CancellationSwallowingTraceProvider:
        async def collect_trace(self, context):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                return await GoodTraceProvider().collect_trace(context)

    workspace = _make_workspace(tmp_path)
    engine = _engine(
        tmp_path,
        policy=_policy(evidence_timeout_ms=100),
        trace_provider=CancellationSwallowingTraceProvider(),
    )

    report = await engine.verify(_request(workspace, engine))
    trace = next(item for item in report.gate_results if item.gate is GateKind.TRACE)

    assert trace.status is GateStatus.ERROR
    assert "TimeoutError" in trace.summary


def test_policy_cannot_relax_article_attempt_or_token_caps() -> None:
    with pytest.raises(ValidationError):
        VerificationRunRequest(
            cycle=4,
            workspace=".",
            control_ref="control",
            control_digest="a" * 64,
            candidate_ref="candidate",
            expected_candidate_digest="d" * 64,
            expected_policy_digest="c" * 64,
            skill_names=("checkout",),
            expected_skill_digests={"checkout": "b" * 64},
        )
    with pytest.raises(ValidationError):
        TraceGateSpec(expected_model="model-a", max_input_tokens=150_001)


def test_policy_rejects_duplicate_global_ui_scenario_ids() -> None:
    scenario = {
        "id": "checkout-page",
        "description": "render checkout",
        "steps": [{"id": "ui", "argv": [sys.executable, "-c", "print('ui')"]}],
    }
    with pytest.raises(ValidationError, match="scenario id"):
        UIGateSpec(mode="required", global_scenarios=(scenario, scenario))


@pytest.mark.asyncio
async def test_skill_and_global_ui_scenario_id_collision_is_blocked(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    loader = _make_skill(tmp_path)
    source = tmp_path / "skills" / "checkout"
    target = tmp_path / "skills" / "global"
    source.rename(target)
    config_path = target / "verification.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["name"] = "global"
    config["ui"] = [
        {
            "id": "same",
            "description": "skill UI",
            "steps": [{"id": "skill-ui", "argv": [sys.executable, "-c", "print('ui')"]}],
        }
    ]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    policy = _policy().model_copy(
        update={
            "ui": UIGateSpec(
                mode="required",
                global_scenarios=(
                    ScenarioSpec(
                        id="same",
                        description="global UI",
                        steps=(
                            CommandSpec(
                                id="global-ui",
                                argv=(sys.executable, "-c", "print('ui')"),
                            ),
                        ),
                    ),
                ),
            )
        }
    )
    engine = VerificationEngine(
        policy=policy,
        skill_loader=loader,
        trace_provider=GoodTraceProvider(),
        log_provider=GoodLogProvider(),
        behavior_provider=GoodBehaviorProvider(),
    )
    skill_digest = loader.load("global").digest
    request = VerificationRunRequest(
        run_id="ui-collision",
        workspace=str(workspace),
        control_ref="control-sha",
        control_digest="e" * 64,
        candidate_ref="candidate-sha",
        expected_candidate_digest=workspace_digest(
            workspace, engine.policy.workspace_ignore
        ),
        expected_policy_digest=engine.policy.digest,
        skill_names=("global",),
        expected_skill_digests={"global": skill_digest},
    )

    report = await engine.verify(request)
    ui = next(item for item in report.gate_results if item.gate is GateKind.UI)

    assert ui.status is GateStatus.BLOCKED
    assert "冲突" in ui.summary


@pytest.mark.asyncio
async def test_behavior_provider_receives_only_behavior_scenarios(tmp_path: Path) -> None:
    seen: dict[str, tuple[str, ...]] = {}

    class RecordingTraceProvider(GoodTraceProvider):
        async def collect_trace(self, context):
            seen["trace"] = context.scenario_ids
            return await super().collect_trace(context)

    class RecordingBehaviorProvider(GoodBehaviorProvider):
        async def collect_behavior(self, context):
            seen["behavior"] = context.scenario_ids
            return await super().collect_behavior(context)

    behavior = BehaviorGateSpec(
        scenarios=(
            BehaviorScenarioSpec(
                scenario_id="behavior-only",
                expected_control_outcome="failure",
                expected_candidate_outcome="success",
                reproducer=True,
            ),
        )
    )
    policy = _policy().model_copy(update={"behavior": behavior})
    workspace = _make_workspace(tmp_path)
    engine = VerificationEngine(
        policy=policy,
        skill_loader=_make_skill(tmp_path),
        trace_provider=RecordingTraceProvider(),
        log_provider=GoodLogProvider(),
        behavior_provider=RecordingBehaviorProvider(),
    )

    await engine.verify(_request(workspace, engine))

    assert set(seen["trace"]) == {"checkout:case", "behavior-only"}
    assert seen["behavior"] == ("behavior-only",)


@pytest.mark.asyncio
async def test_json_store_is_atomic_and_refuses_overwrite(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    engine = _engine(tmp_path)
    report = await engine.verify(_request(workspace, engine))
    store = JsonEvidenceStore(tmp_path / "evidence")
    location = Path(await store.persist(report))
    payload = json.loads((location / "report.json").read_text(encoding="utf-8"))
    assert payload["verdict"] == "verified"
    assert (location / "external-evidence.json").is_file()
    assert (location / "command-evidence.json").is_file()
    with pytest.raises(FileExistsError):
        await store.persist(report)


@pytest.mark.asyncio
async def test_attested_store_binds_report_to_application_and_detects_tampering(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    engine = _engine(tmp_path)
    report = await engine.verify(_request(workspace, engine))
    key = b"k" * 32
    root = tmp_path / "trusted-evidence"
    store = AttestedJsonEvidenceStore(
        root,
        signing_key=key,
        app_id="ccb",
        repository="acme/ccb",
    )
    location = Path(await store.persist(report))

    restored, path = AttestedJsonEvidenceStore.load_attested(
        root,
        run_id=report.run_id,
        cycle=report.cycle,
        signing_key=key,
        app_id="ccb",
        repository="acme/ccb",
        policy_digest=report.policy_digest,
        skill_digests=report.skill_digests,
    )
    assert restored == report
    assert Path(path) == location / "report.json"

    report_path = location / "report.json"
    report_path.write_bytes(report_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="签名无效"):
        AttestedJsonEvidenceStore.load_attested(
            root,
            run_id=report.run_id,
            cycle=report.cycle,
            signing_key=key,
            app_id="ccb",
            repository="acme/ccb",
            policy_digest=report.policy_digest,
            skill_digests=report.skill_digests,
        )


def test_report_rejects_forged_verified_verdict() -> None:
    policy = VerificationPolicy()
    gates = tuple(
        GateResult(gate=kind, status=GateStatus.PASS, summary="claimed pass")
        for kind in GateKind
    )
    with pytest.raises(ValidationError):
        VerificationReport(
            run_id="safe-run",
            cycle=1,
            control_ref="control",
            control_digest="a" * 64,
            candidate_ref="candidate",
            candidate_digest="b" * 64,
            candidate_digest_after="b" * 64,
            skill_names=("checkout",),
            skill_digests={"checkout": "c" * 64},
            policy=policy,
            policy_digest=policy.digest,
            gate_results=gates,
            verdict=VerificationVerdict.VERIFIED,
        )


def test_report_and_request_reject_path_traversal_run_id(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="run_id"):
        VerificationRunRequest(
            run_id="../escape",
            workspace=str(tmp_path),
            control_ref="control",
            control_digest="a" * 64,
            candidate_ref="candidate",
            expected_candidate_digest="d" * 64,
            expected_policy_digest="c" * 64,
            skill_names=("checkout",),
            expected_skill_digests={"checkout": "b" * 64},
        )
