from __future__ import annotations

from hashlib import sha256
import re

import pytest
from pydantic import ValidationError

from core.verification import (
    ARTICLE_LINT_WARNING_PATTERNS,
    BehaviorEvidence,
    BehaviorGateSpec,
    BehaviorObservation,
    BehaviorScenarioSpec,
    CommandEvidence,
    CommandSpec,
    GateKind,
    GateResult,
    GateStatus,
    LintGateSpec,
    LogEvidence,
    LogGateSpec,
    LogObservation,
    ScenarioCollection,
    TraceEvidence,
    TraceObservation,
    UnitGateSpec,
    Variant,
    VerificationPolicy,
    VerificationReport,
    VerificationVerdict,
)
from core.verification.gates import (
    _changed_paths,
    _path_matches,
    evaluate_behavior_gate,
    evaluate_command_gate,
    evaluate_log_gate,
)
from core.verification.runner import command_contract_digest


RUN_ID = "run-hardening"
SCENARIO_ID = "checkout:submit"
CONTROL_REF = "control-ref"
CANDIDATE_REF = "candidate-ref"
CONTROL_DIGEST = "a" * 64
CANDIDATE_DIGEST = "b" * 64
POLICY_DIGEST = "c" * 64
SKILL_DIGEST = "d" * 64
EMPTY_SHA256 = sha256(b"").hexdigest()


def test_trace_observation_rejects_blank_request_id() -> None:
    with pytest.raises(ValidationError):
        TraceObservation(
            trace_id="trace-1",
            request_id="   ",
            scenario_id=SCENARIO_ID,
        )


def _command_evidence(
    *,
    evidence_id: str,
    gate: GateKind,
    spec: CommandSpec,
    contract_digest: str | None = None,
    skill_name: str | None = None,
    scenario_id: str | None = None,
) -> CommandEvidence:
    return CommandEvidence(
        evidence_id=evidence_id,
        run_id=RUN_ID,
        cycle=1,
        gate=gate,
        check_id=spec.id,
        scenario_id=scenario_id,
        skill_name=skill_name,
        policy_digest=POLICY_DIGEST,
        skill_digest=SKILL_DIGEST if skill_name else None,
        command_spec_digest=(
            contract_digest
            if contract_digest is not None
            else command_contract_digest(spec, ())
        ),
        command_spec=spec,
        candidate_ref=CANDIDATE_REF,
        candidate_digest_before=CANDIDATE_DIGEST,
        candidate_digest_after=CANDIDATE_DIGEST,
        argv=spec.argv,
        cwd=spec.cwd,
        exit_code=spec.expected_exit_code,
        stdout="",
        stderr="",
        stdout_sha256=EMPTY_SHA256,
        stderr_sha256=EMPTY_SHA256,
        stdout_bytes=0,
        stderr_bytes=0,
        sandbox_backend="macos-seatbelt+workspace-copy",
        duration_ms=1,
        expected_exit_code=spec.expected_exit_code,
        expected_stdout_contains=spec.stdout_contains,
        expected_stderr_contains=spec.stderr_contains,
        forbidden_output_patterns=spec.forbidden_output_patterns,
        passed=True,
    )


def _behavior_spec() -> BehaviorGateSpec:
    return BehaviorGateSpec(
        scenarios=(
            BehaviorScenarioSpec(
                scenario_id=SCENARIO_ID,
                expected_control_outcome="failure",
                expected_candidate_outcome="success",
                allowed_changed_paths=("$.status",),
                reproducer=True,
            ),
        )
    )


def _behavior_evidence(
    *,
    collected_scenarios: tuple[ScenarioCollection, ...] | None = None,
    candidate_updates: dict[str, object] | None = None,
) -> BehaviorEvidence:
    candidate = {
        "observation_id": "behavior-candidate",
        "scenario_id": SCENARIO_ID,
        "variant": Variant.CANDIDATE,
        "outcome": "success",
        "payload": {"status": "fixed"},
        "model": "model-a",
        "tool_calls": (
            {
                "sequence": 0,
                "tool_name": "checkout",
                "input_digest": "1" * 64,
                "output_digest": "2" * 64,
                "outcome": "success",
            },
        ),
        "finished": True,
        "request_id": "request-candidate",
        "trace_id": "trace-candidate",
    }
    candidate.update(candidate_updates or {})
    if collected_scenarios is None:
        collected_scenarios = (
            ScenarioCollection(
                scenario_id=SCENARIO_ID,
                variant=Variant.CONTROL,
                collection_id="behavior-control",
                input_digest="e" * 64,
            ),
            ScenarioCollection(
                scenario_id=SCENARIO_ID,
                variant=Variant.CANDIDATE,
                collection_id="behavior-candidate",
                input_digest="e" * 64,
            ),
        )
    return BehaviorEvidence(
        run_id=RUN_ID,
        control_ref=CONTROL_REF,
        control_digest=CONTROL_DIGEST,
        candidate_ref=CANDIDATE_REF,
        candidate_digest=CANDIDATE_DIGEST,
        policy_digest=POLICY_DIGEST,
        skill_digests={"checkout": SKILL_DIGEST},
        collection_complete=True,
        collected_variants=(Variant.CONTROL, Variant.CANDIDATE),
        collected_scenarios=collected_scenarios,
        observations=(
            BehaviorObservation(
                observation_id="behavior-control",
                scenario_id=SCENARIO_ID,
                variant=Variant.CONTROL,
                outcome="failure",
                payload={"status": "broken"},
                input_digest="e" * 64,
                model="model-a",
                tool_calls=(
                    {
                        "sequence": 0,
                        "tool_name": "checkout",
                        "input_digest": "1" * 64,
                        "output_digest": "2" * 64,
                        "outcome": "success",
                    },
                ),
                finished=True,
                request_id="request-control",
                trace_id="trace-control",
            ),
            BehaviorObservation(input_digest="e" * 64, **candidate),
        ),
    )


def _log_evidence(
    *,
    collected_scenarios: tuple[ScenarioCollection, ...] | None = None,
    candidate_level: str = "INFO",
    candidate_error_type: str = "NoError",
) -> LogEvidence:
    if collected_scenarios is None:
        collected_scenarios = (
            ScenarioCollection(
                scenario_id=SCENARIO_ID,
                variant=Variant.CONTROL,
                collection_id="logs-control",
                input_digest="e" * 64,
            ),
            ScenarioCollection(
                scenario_id=SCENARIO_ID,
                variant=Variant.CANDIDATE,
                collection_id="logs-candidate",
                input_digest="e" * 64,
            ),
        )
    return LogEvidence(
        run_id=RUN_ID,
        control_ref=CONTROL_REF,
        control_digest=CONTROL_DIGEST,
        candidate_ref=CANDIDATE_REF,
        candidate_digest=CANDIDATE_DIGEST,
        policy_digest=POLICY_DIGEST,
        skill_digests={"checkout": SKILL_DIGEST},
        collection_complete=True,
        collected_variants=(Variant.CONTROL, Variant.CANDIDATE),
        collected_scenarios=collected_scenarios,
        observations=(
            LogObservation(
                observation_id="log-control",
                scenario_id=SCENARIO_ID,
                variant=Variant.CONTROL,
                service="api",
                level="INFO",
                error_type="NoError",
                request_id="request-control",
                trace_id="trace-control",
            ),
            LogObservation(
                observation_id="log-candidate",
                scenario_id=SCENARIO_ID,
                variant=Variant.CANDIDATE,
                service="api",
                level=candidate_level,
                error_type=candidate_error_type,
                request_id="request-candidate",
                trace_id="trace-candidate",
            ),
        ),
    )


def _trace_evidence(*, error_observations: int = 0) -> TraceEvidence:
    return TraceEvidence(
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        candidate_digest=CANDIDATE_DIGEST,
        policy_digest=POLICY_DIGEST,
        skill_digests={"checkout": SKILL_DIGEST},
        collection_complete=True,
        observations=(
            TraceObservation(
                trace_id="trace-candidate",
                request_id="request-candidate",
                scenario_id=SCENARIO_ID,
                input_digest="e" * 64,
                error_observations=error_observations,
                actual_model="model-a",
                fallback_used=False,
                input_tokens=10,
                finished=True,
            ),
        ),
    )


@pytest.mark.parametrize(
    "evidence_factory",
    [_trace_evidence, _log_evidence, _behavior_evidence],
)
def test_external_evidence_rejects_blank_collector_error(evidence_factory) -> None:
    evidence = evidence_factory()
    payload = evidence.model_dump(mode="python")
    payload["collector_error"] = "   "
    with pytest.raises(ValidationError, match="collector_error"):
        type(evidence).model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("collection_complete", "yes"),
        ("error_observations", "0"),
        ("fallback_used", "false"),
        ("input_tokens", "10"),
        ("finished", "yes"),
    ),
)
def test_trace_evidence_rejects_coerced_gate_values(field: str, value: str) -> None:
    payload = _trace_evidence().model_dump(mode="python")
    if field == "collection_complete":
        payload[field] = value
    else:
        payload["observations"][0][field] = value
    with pytest.raises(ValidationError):
        TraceEvidence.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("cycle", "1"),
        ("exit_code", "0"),
        ("stdout_bytes", "0"),
        ("stdout_truncated", "false"),
        ("duration_ms", "1"),
        ("timed_out", "false"),
        ("expected_exit_code", "0"),
        ("passed", "true"),
    ),
)
def test_command_evidence_rejects_coerced_result_values(
    field: str, value: str
) -> None:
    spec = CommandSpec(id="unit", argv=("python", "-m", "pytest"))
    payload = _command_evidence(
        evidence_id="strict-command",
        gate=GateKind.UNIT,
        spec=spec,
    ).model_dump(mode="python")
    payload[field] = value
    with pytest.raises(ValidationError):
        CommandEvidence.model_validate(payload)


def _evaluate_behavior(evidence: BehaviorEvidence) -> GateResult:
    return evaluate_behavior_gate(
        _behavior_spec(),
        evidence,
        run_id=RUN_ID,
        cycle=1,
        control_ref=CONTROL_REF,
        control_digest=CONTROL_DIGEST,
        candidate_ref=CANDIDATE_REF,
        candidate_digest=CANDIDATE_DIGEST,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests={"checkout": SKILL_DIGEST},
        candidate_traces={
            "trace-candidate": _trace_evidence().observations[0],
        },
    )


def test_command_gate_rejects_substituted_argv_contract() -> None:
    frozen = CommandSpec(
        id="lint",
        argv=("python", "-m", "compileall", "."),
    )
    substituted = CommandSpec(
        id="lint",
        argv=("python", "-c", "print('pretend pass')"),
    )
    key = (None, None, "lint")
    evidence = _command_evidence(
        evidence_id="command-lint",
        gate=GateKind.LINT,
        spec=substituted,
    )

    result = evaluate_command_gate(
        GateKind.LINT,
        (evidence,),
        expected_contracts={key: command_contract_digest(frozen, ())},
        run_id=RUN_ID,
        cycle=1,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests={},
        candidate_ref=CANDIDATE_REF,
        candidate_digest=CANDIDATE_DIGEST,
    )

    assert evidence.argv != frozen.argv
    assert result.status is GateStatus.BLOCKED


@pytest.mark.parametrize("gate", ["lint", "unit"])
def test_policy_cannot_expect_nonzero_exit_for_lint_or_unit(gate: str) -> None:
    check = CommandSpec(id=gate, argv=("python", "-m", "pytest"), expected_exit_code=1)

    with pytest.raises(ValidationError):
        if gate == "lint":
            VerificationPolicy(lint=LintGateSpec(checks=(check,)))
        else:
            VerificationPolicy(unit=UnitGateSpec(checks=(check,)))


@pytest.mark.parametrize("field", ["stdout_contains", "stderr_contains"])
def test_command_spec_rejects_blank_output_marker(field: str) -> None:
    with pytest.raises(ValidationError, match="标记不能为空"):
        CommandSpec(id="check", argv=("python",), **{field: ("   ",)})


def test_policy_cannot_remove_error_from_log_levels() -> None:
    with pytest.raises(ValidationError):
        VerificationPolicy(staging_log=LogGateSpec(error_levels=("FATAL",)))


def test_policy_cannot_replace_builtin_zero_warning_detection() -> None:
    spec = LintGateSpec(
        checks=(CommandSpec(id="lint", argv=("python", "-m", "compileall", ".")),),
        warning_patterns=(r"(?!x)x",),
    )

    assert spec.warning_patterns[: len(ARTICLE_LINT_WARNING_PATTERNS)] == (
        ARTICLE_LINT_WARNING_PATTERNS
    )


@pytest.mark.parametrize(
    "output",
    (
        "WARN unused import",
        "warning_count=2",
        "0 warnings; warning: unused import",
    ),
)
def test_builtin_lint_detection_rejects_common_warning_formats(output: str) -> None:
    assert any(re.search(pattern, output) for pattern in ARTICLE_LINT_WARNING_PATTERNS)


@pytest.mark.parametrize(
    "output",
    (
        "0 warnings",
        "Warnings: 0",
        "2 passed, 0 warnings in 1.2s",
        "✖ 0 problems (0 errors, 0 warnings)",
        "Warnings (0)",
        "Build completed with 0 warnings.",
    ),
)
def test_builtin_lint_detection_accepts_zero_warning_summaries(output: str) -> None:
    assert not any(re.search(pattern, output) for pattern in ARTICLE_LINT_WARNING_PATTERNS)


def test_policy_rejects_broad_workspace_ignore() -> None:
    with pytest.raises(ValidationError):
        VerificationPolicy(workspace_ignore=("src/**",))


def test_policy_cannot_disable_behavior_trace_shape() -> None:
    with pytest.raises(ValidationError):
        VerificationPolicy(
            behavior=BehaviorGateSpec(
                scenarios=(
                    BehaviorScenarioSpec(
                        scenario_id=SCENARIO_ID,
                        expected_control_outcome="failure",
                        reproducer=True,
                        require_trace_shape=False,
                    ),
                )
            )
        )


def test_policy_requires_successful_candidate_behavior() -> None:
    with pytest.raises(ValidationError):
        BehaviorScenarioSpec(
            scenario_id=SCENARIO_ID,
            expected_control_outcome="success",
            expected_candidate_outcome="failure",
            reproducer=True,
        )


def test_behavior_outcome_change_requires_explicit_reproducer() -> None:
    with pytest.raises(ValidationError, match="reproducer"):
        BehaviorScenarioSpec(
            scenario_id=SCENARIO_ID,
            expected_control_outcome="failure",
            expected_candidate_outcome="success",
        )


def test_behavior_diff_paths_are_unambiguous_and_leaf_scoped() -> None:
    assert _changed_paths(
        {"a": {"b": 1}, "a.b": 0},
        {"a": {"b": 2}, "a.b": 0},
    ) == {"$.a.b"}
    assert _changed_paths(
        {"a": {"b": 1}, "a.b": 0},
        {"a": {"b": 1}, "a.b": 2},
    ) == {"$.{612e62}"}
    assert _changed_paths({"body": {}}, {"body": {"ok": True}}) == {
        "$.body.ok"
    }


@pytest.mark.parametrize(
    ("control", "candidate"),
    [(True, 1), (1, 1.0), (False, 0)],
)
def test_behavior_diff_detects_equal_python_values_with_different_json_types(
    control: object, candidate: object
) -> None:
    assert _changed_paths(control, candidate) == {"$"}


def test_behavior_path_globs_treat_array_brackets_as_literals() -> None:
    assert _path_matches("$.items[0]", "$.items[0]")
    assert _path_matches("$.items[12].id", "$.items[*].id")


@pytest.mark.parametrize("reserved_path", ["@model", "@tool_calls", "@finished"])
def test_policy_cannot_allow_reserved_behavior_trace_changes(
    reserved_path: str,
) -> None:
    with pytest.raises(ValidationError):
        VerificationPolicy(
            behavior=BehaviorGateSpec(
                scenarios=(
                    BehaviorScenarioSpec(
                        scenario_id=SCENARIO_ID,
                        expected_control_outcome="failure",
                        allowed_changed_paths=(reserved_path,),
                        reproducer=True,
                    ),
                )
            )
        )


@pytest.mark.parametrize("cycle", [0, 4])
def test_report_cycle_is_limited_to_article_attempts(cycle: int) -> None:
    policy = VerificationPolicy()
    with pytest.raises(ValidationError):
        VerificationReport(
            run_id=RUN_ID,
            cycle=cycle,
            control_ref=CONTROL_REF,
            control_digest=CONTROL_DIGEST,
            candidate_ref=CANDIDATE_REF,
            skill_names=("checkout",),
            policy=policy,
            policy_digest=policy.digest,
            gate_results=(),
            verdict=VerificationVerdict.ERROR,
        )


def test_log_gate_blocks_incomplete_collected_scenario_coverage() -> None:
    result = evaluate_log_gate(
        LogGateSpec(),
        _log_evidence(collected_scenarios=()),
        run_id=RUN_ID,
        cycle=1,
        control_ref=CONTROL_REF,
        control_digest=CONTROL_DIGEST,
        candidate_ref=CANDIDATE_REF,
        candidate_digest=CANDIDATE_DIGEST,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests={"checkout": SKILL_DIGEST},
        scenario_ids={SCENARIO_ID},
        scenario_input_digests={SCENARIO_ID: "e" * 64},
    )

    assert result.status is GateStatus.BLOCKED


def test_log_gate_requires_same_frozen_input_as_trace() -> None:
    evidence = _log_evidence()
    bad_windows = (
        evidence.collected_scenarios[0],
        evidence.collected_scenarios[1].model_copy(
            update={"input_digest": "f" * 64}
        ),
    )
    result = evaluate_log_gate(
        LogGateSpec(),
        evidence.model_copy(update={"collected_scenarios": bad_windows}),
        run_id=RUN_ID,
        cycle=1,
        control_ref=CONTROL_REF,
        control_digest=CONTROL_DIGEST,
        candidate_ref=CANDIDATE_REF,
        candidate_digest=CANDIDATE_DIGEST,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests={"checkout": SKILL_DIGEST},
        scenario_ids={SCENARIO_ID},
        scenario_input_digests={SCENARIO_ID: "e" * 64},
    )

    assert result.status is GateStatus.BLOCKED


def test_log_fingerprint_always_includes_error_type() -> None:
    evidence = _log_evidence(candidate_level="ERROR", candidate_error_type="DatabaseError")
    observations = tuple(
        item.model_copy(
            update={
                "level": "ERROR",
                "event_code": "E-1",
                "message_template": "operation failed",
            }
        )
        for item in evidence.observations
    )
    result = evaluate_log_gate(
        LogGateSpec(),
        evidence.model_copy(update={"observations": observations}),
        run_id=RUN_ID,
        cycle=1,
        control_ref=CONTROL_REF,
        control_digest=CONTROL_DIGEST,
        candidate_ref=CANDIDATE_REF,
        candidate_digest=CANDIDATE_DIGEST,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests={"checkout": SKILL_DIGEST},
        scenario_ids={SCENARIO_ID},
        scenario_input_digests={SCENARIO_ID: "e" * 64},
    )

    assert result.status is GateStatus.FAIL
    assert "DatabaseError" in result.failures[0]


def test_behavior_gate_blocks_incomplete_collected_scenario_coverage() -> None:
    result = _evaluate_behavior(_behavior_evidence(collected_scenarios=()))

    assert result.status is GateStatus.BLOCKED


def test_external_evidence_must_bind_frozen_skill_digests() -> None:
    evidence = _behavior_evidence().model_copy(
        update={"skill_digests": {"checkout": "f" * 64}}
    )

    assert _evaluate_behavior(evidence).status is GateStatus.BLOCKED


def test_behavior_identity_must_match_referenced_trace() -> None:
    evidence = _behavior_evidence(
        candidate_updates={"request_id": "different-request"}
    )

    assert _evaluate_behavior(evidence).status is GateStatus.BLOCKED


def test_command_evidence_rejects_forged_output_hash() -> None:
    evidence = _command_evidence(
        evidence_id="command-hash",
        gate=GateKind.UNIT,
        spec=CommandSpec(id="unit", argv=("python", "-m", "pytest")),
    )
    payload = evidence.model_dump(mode="python")
    payload["stdout_sha256"] = "f" * 64

    with pytest.raises(ValidationError):
        CommandEvidence.model_validate(payload)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_command_evidence_cannot_pass_with_truncated_output(stream: str) -> None:
    evidence = _command_evidence(
        evidence_id=f"command-{stream}-truncated",
        gate=GateKind.UNIT,
        spec=CommandSpec(id="unit", argv=("python", "-m", "pytest")),
    )
    payload = evidence.model_dump(mode="python")
    payload[f"{stream}_truncated"] = True
    payload[f"{stream}_bytes"] = 1
    payload[f"{stream}_sha256"] = "f" * 64

    with pytest.raises(ValidationError):
        CommandEvidence.model_validate(payload)


def test_command_evidence_cannot_pass_without_required_sandbox() -> None:
    evidence = _command_evidence(
        evidence_id="command-sandbox",
        gate=GateKind.UNIT,
        spec=CommandSpec(id="unit", argv=("python", "-m", "pytest")),
    )
    payload = evidence.model_dump(mode="python")
    payload["sandbox_backend"] = "unavailable"

    with pytest.raises(ValidationError):
        CommandEvidence.model_validate(payload)


@pytest.mark.parametrize("missing_field", ["finished", "request_id", "trace_id"])
def test_candidate_behavior_missing_identity_or_completion_is_blocked(
    missing_field: str,
) -> None:
    result = _evaluate_behavior(
        _behavior_evidence(candidate_updates={missing_field: None})
    )

    assert result.status is GateStatus.BLOCKED


def test_candidate_behavior_must_reference_collected_trace() -> None:
    evidence = _behavior_evidence(
        candidate_updates={"trace_id": "unrelated-trace"}
    )

    assert _evaluate_behavior(evidence).status is GateStatus.BLOCKED
