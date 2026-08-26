from __future__ import annotations

import pytest

from core.verification import (
    BehaviorEvidence,
    BehaviorGateSpec,
    BehaviorObservation,
    BehaviorScenarioSpec,
    GateKind,
    GateResult,
    GateStatus,
    LogEvidence,
    LogGateSpec,
    LogObservation,
    ScenarioCollection,
    TraceEvidence,
    TraceGateSpec,
    TraceObservation,
    Variant,
    VerificationVerdict,
    aggregate_verdict,
)
from core.verification.gates import (
    evaluate_behavior_gate,
    evaluate_log_gate,
    evaluate_trace_gate,
)


DIGEST = "a" * 64
CONTROL_DIGEST = "b" * 64
POLICY_DIGEST = "c" * 64
SKILL_DIGESTS = {"checkout": "f" * 64}


def _trace(**updates) -> TraceEvidence:
    values = {
        "run_id": "run-1",
        "candidate_ref": "candidate",
        "candidate_digest": DIGEST,
        "policy_digest": POLICY_DIGEST,
        "skill_digests": SKILL_DIGESTS,
        "collection_complete": True,
        "observations": (
            TraceObservation(
                trace_id="trace-1",
                request_id="request-candidate",
                scenario_id="checkout:submit",
                input_digest="d" * 64,
                error_observations=0,
                actual_model="model-a",
                fallback_used=False,
                input_tokens=149_999,
                finished=True,
            ),
        ),
    }
    values.update(updates)
    return TraceEvidence(**values)


def _trace_result(evidence: TraceEvidence) -> GateResult:
    return evaluate_trace_gate(
        TraceGateSpec(expected_model="model-a"),
        evidence,
        run_id="run-1",
        cycle=1,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests=SKILL_DIGESTS,
        scenario_ids={"checkout:submit"},
    )


def test_trace_article_threshold_is_strict() -> None:
    assert _trace_result(_trace()).status is GateStatus.PASS
    at_limit = _trace(
        observations=(
            TraceObservation(
                trace_id="trace-1",
                request_id="request-candidate",
                scenario_id="checkout:submit",
                input_digest="d" * 64,
                error_observations=0,
                actual_model="model-a",
                fallback_used=False,
                input_tokens=150_000,
                finished=True,
            ),
        )
    )
    result = _trace_result(at_limit)
    assert result.status is GateStatus.FAIL
    assert "must be < 150000" in result.failures[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("error_observations", 1),
        ("actual_model", "wrong-model"),
        ("fallback_used", True),
        ("finished", False),
    ],
)
def test_each_trace_hard_metric_can_fail(field: str, value: object) -> None:
    observation = _trace().observations[0].model_copy(update={field: value})
    result = _trace_result(_trace(observations=(observation,)))
    assert result.status is GateStatus.FAIL


def test_missing_trace_field_or_provider_collection_is_blocked() -> None:
    observation = _trace().observations[0].model_copy(update={"actual_model": None})
    assert _trace_result(_trace(observations=(observation,))).status is GateStatus.BLOCKED
    assert _trace_result(_trace(collection_complete=False)).status is GateStatus.BLOCKED


def test_log_gate_rejects_only_new_candidate_error_types() -> None:
    common = dict(
        run_id="run-1",
        control_ref="control",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        skill_digests=SKILL_DIGESTS,
        collection_complete=True,
        collected_variants=(Variant.CONTROL, Variant.CANDIDATE),
        collected_scenarios=(
            ScenarioCollection(
                scenario_id="s",
                variant=Variant.CONTROL,
                collection_id="logs-control-s",
                input_digest="d" * 64,
            ),
            ScenarioCollection(
                scenario_id="s",
                variant=Variant.CANDIDATE,
                collection_id="logs-candidate-s",
                input_digest="d" * 64,
            ),
        ),
    )
    known = LogEvidence(
        **common,
        observations=(
            LogObservation(
                scenario_id="s",
                variant=Variant.CONTROL,
                service="api",
                level="ERROR",
                error_type="TimeoutError",
                request_id="control-request",
            ),
            LogObservation(
                scenario_id="s",
                variant=Variant.CANDIDATE,
                service="api",
                level="ERROR",
                error_type="TimeoutError",
                request_id="candidate-request",
            ),
        ),
    )
    assert evaluate_log_gate(
        LogGateSpec(),
        known,
        run_id="run-1",
        cycle=1,
        control_ref="control",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests=SKILL_DIGESTS,
        scenario_ids={"s"},
        scenario_input_digests={"s": "d" * 64},
    ).status is GateStatus.PASS

    added = known.model_copy(
        update={
            "observations": (*known.observations, LogObservation(
                scenario_id="s",
                variant=Variant.CANDIDATE,
                service="api",
                level="ERROR",
                error_type="DatabaseError",
                request_id="candidate-request-2",
            ))
        }
    )
    result = evaluate_log_gate(
        LogGateSpec(),
        added,
        run_id="run-1",
        cycle=1,
        control_ref="control",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests=SKILL_DIGESTS,
        scenario_ids={"s"},
        scenario_input_digests={"s": "d" * 64},
    )
    assert result.status is GateStatus.FAIL
    assert "DatabaseError" in result.failures[0]

    moved_between_scenarios = LogEvidence(
        **{
            **common,
            "collected_scenarios": (
                ScenarioCollection(
                    scenario_id="old-scenario",
                    variant=Variant.CONTROL,
                    collection_id="logs-control-old",
                    input_digest="d" * 64,
                ),
                ScenarioCollection(
                    scenario_id="old-scenario",
                    variant=Variant.CANDIDATE,
                    collection_id="logs-candidate-old",
                    input_digest="d" * 64,
                ),
                ScenarioCollection(
                    scenario_id="new-scenario",
                    variant=Variant.CONTROL,
                    collection_id="logs-control-new",
                    input_digest="d" * 64,
                ),
                ScenarioCollection(
                    scenario_id="new-scenario",
                    variant=Variant.CANDIDATE,
                    collection_id="logs-candidate-new",
                    input_digest="d" * 64,
                ),
            ),
        },
        observations=(
            LogObservation(
                scenario_id="old-scenario",
                variant=Variant.CONTROL,
                service="api",
                level="ERROR",
                error_type="TimeoutError",
                request_id="old-request",
            ),
            LogObservation(
                scenario_id="new-scenario",
                variant=Variant.CANDIDATE,
                service="api",
                level="ERROR",
                error_type="TimeoutError",
                request_id="new-request",
            ),
        ),
    )
    assert evaluate_log_gate(
        LogGateSpec(),
        moved_between_scenarios,
        run_id="run-1",
        cycle=1,
        control_ref="control",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests=SKILL_DIGESTS,
        scenario_ids={"old-scenario", "new-scenario"},
        scenario_input_digests={
            "old-scenario": "d" * 64,
            "new-scenario": "d" * 64,
        },
    ).status is GateStatus.FAIL


def test_behavior_pairs_by_run_and_scenario_not_request_or_trace_id() -> None:
    spec = BehaviorGateSpec(
        scenarios=(
            BehaviorScenarioSpec(
                scenario_id="checkout:submit",
                allowed_changed_paths=("$.status",),
                expected_control_outcome="failure",
                reproducer=True,
            ),
        )
    )
    evidence = BehaviorEvidence(
        run_id="run-1",
        control_ref="control",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        skill_digests=SKILL_DIGESTS,
        collection_complete=True,
        collected_variants=(Variant.CONTROL, Variant.CANDIDATE),
        collected_scenarios=(
            ScenarioCollection(
                scenario_id="checkout:submit",
                variant=Variant.CONTROL,
                collection_id="behavior-control-checkout",
                input_digest="e" * 64,
            ),
            ScenarioCollection(
                scenario_id="checkout:submit",
                variant=Variant.CANDIDATE,
                collection_id="behavior-candidate-checkout",
                input_digest="e" * 64,
            ),
        ),
        observations=(
            BehaviorObservation(
                scenario_id="checkout:submit",
                variant=Variant.CONTROL,
                outcome="failure",
                payload={"status": "old", "total": 42},
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
                request_id="old-request",
                trace_id="old-trace",
            ),
            BehaviorObservation(
                scenario_id="checkout:submit",
                variant=Variant.CANDIDATE,
                outcome="success",
                payload={"status": "new", "total": 42},
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
                request_id="request-new",
                trace_id="trace-new",
            ),
        ),
    )
    result = evaluate_behavior_gate(
        spec,
        evidence,
        run_id="run-1",
        cycle=1,
        control_ref="control",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests=SKILL_DIGESTS,
        candidate_traces={
            "trace-new": TraceObservation(
                trace_id="trace-new",
                request_id="request-new",
                scenario_id="checkout:submit",
                input_digest="e" * 64,
                error_observations=0,
                actual_model="model-a",
                fallback_used=False,
                input_tokens=1,
                finished=True,
            )
        },
    )
    assert result.status is GateStatus.PASS

    changed_total = evidence.model_copy(
        update={
            "observations": (
                evidence.observations[0],
                evidence.observations[1].model_copy(
                    update={"payload": {"status": "new", "total": 41}}
                ),
            )
        }
    )
    assert evaluate_behavior_gate(
        spec,
        changed_total,
        run_id="run-1",
        cycle=1,
        control_ref="control",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests=SKILL_DIGESTS,
        candidate_traces={
            "trace-new": TraceObservation(
                trace_id="trace-new",
                request_id="request-new",
                scenario_id="checkout:submit",
                input_digest="e" * 64,
                error_observations=0,
                actual_model="model-a",
                fallback_used=False,
                input_tokens=1,
                finished=True,
            )
        },
    ).status is GateStatus.FAIL


def test_behavior_requires_declared_reproducer_and_trace_shape() -> None:
    with pytest.raises(ValueError, match="reproducer"):
        BehaviorGateSpec(
            scenarios=(BehaviorScenarioSpec(scenario_id="same"),)
        )

    spec = BehaviorGateSpec(
        scenarios=(
            BehaviorScenarioSpec(
                scenario_id="fix",
                expected_control_outcome="failure",
                reproducer=True,
            ),
        )
    )
    evidence = BehaviorEvidence(
        run_id="run-1",
        control_ref="control",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        skill_digests=SKILL_DIGESTS,
        collection_complete=True,
        collected_variants=(Variant.CONTROL, Variant.CANDIDATE),
        collected_scenarios=(
            ScenarioCollection(
                scenario_id="fix",
                variant=Variant.CONTROL,
                collection_id="behavior-control-fix",
                input_digest="e" * 64,
            ),
            ScenarioCollection(
                scenario_id="fix",
                variant=Variant.CANDIDATE,
                collection_id="behavior-candidate-fix",
                input_digest="e" * 64,
            ),
        ),
        observations=(
            BehaviorObservation(
                scenario_id="fix",
                variant=Variant.CONTROL,
                outcome="failure",
                payload={},
            ),
            BehaviorObservation(
                scenario_id="fix",
                variant=Variant.CANDIDATE,
                outcome="success",
                payload={},
            ),
        ),
    )
    assert evaluate_behavior_gate(
        spec,
        evidence,
        run_id="run-1",
        cycle=1,
        control_ref="control",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        expected_skill_digests=SKILL_DIGESTS,
        candidate_traces={},
    ).status is GateStatus.BLOCKED


def test_aggregate_requires_all_seven_gates() -> None:
    six = [
        GateResult(gate=kind, status=GateStatus.PASS, summary="ok")
        for kind in GateKind
        if kind is not GateKind.UI
    ]
    assert aggregate_verdict(six) is VerificationVerdict.ERROR

    all_results = [
        *six,
        GateResult(
            gate=GateKind.UI,
            status=GateStatus.NOT_APPLICABLE,
            summary="api only",
        ),
    ]
    assert aggregate_verdict(all_results) is VerificationVerdict.VERIFIED
    all_results[0] = GateResult(
        gate=GateKind.LINT,
        status=GateStatus.FAIL,
        summary="warning",
        failures=("warning",),
    )
    assert aggregate_verdict(all_results) is VerificationVerdict.REJECTED
