from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.verification import FailureSignature
from core.verification.replay import ReplayResult
from core.verification.replay_oracle import (
    HostReplayOracle,
    RawReplayLog,
    RawReplayResponse,
)


def test_oracle_computes_semantics_from_raw_response_and_logs() -> None:
    signature = FailureSignature(
        code="checkout.timeout",
        error_type="TimeoutError",
        message_pattern=r"checkout .* timed out",
        event_code="CHECKOUT_TIMEOUT",
    )
    decision = HostReplayOracle.evaluate(
        response=RawReplayResponse(
            status_code=504,
            body={"error": "upstream timeout", "retryable": True},
        ),
        logs=(
            RawReplayLog(
                level="ERROR",
                error_type="TimeoutError",
                message="checkout request timed out after 3s",
                event_code="CHECKOUT_TIMEOUT",
            ),
        ),
        signatures=(signature,),
    )

    assert decision.outcome == "failure"
    assert decision.failure_signatures == ("checkout.timeout",)
    assert decision.payload == {"error": "upstream timeout", "retryable": True}
    assert len(decision.digest) == 64


@pytest.mark.parametrize(
    "changed_log",
    [
        {"error_type": "ValueError"},
        {"message": "checkout was cancelled"},
        {"event_code": "OTHER_EVENT"},
    ],
)
def test_signature_requires_all_matchers_on_the_same_diagnostic(
    changed_log: dict[str, str],
) -> None:
    signature = FailureSignature(
        code="checkout.timeout",
        error_type="TimeoutError",
        message_pattern=r"timed out",
        event_code="CHECKOUT_TIMEOUT",
    )
    raw_log = {
        "level": "ERROR",
        "error_type": "TimeoutError",
        "message": "checkout timed out",
        "event_code": "CHECKOUT_TIMEOUT",
        **changed_log,
    }

    decision = HostReplayOracle.evaluate(
        response=RawReplayResponse(status_code=500, body={"ok": False}),
        logs=(RawReplayLog.model_validate(raw_log),),
        signatures=(signature,),
    )

    assert decision.outcome == "failure"
    assert decision.failure_signatures == ()


def test_success_response_with_error_log_is_failure() -> None:
    decision = HostReplayOracle.evaluate(
        response=RawReplayResponse(status_code=200, body={"ok": True}),
        logs=(RawReplayLog(level="ERROR", message="hidden failure"),),
    )

    assert decision.outcome == "failure"


def test_redirect_response_is_not_implicitly_successful() -> None:
    decision = HostReplayOracle.evaluate(
        response=RawReplayResponse(
            status_code=302,
            body={"location": "/login"},
        ),
        logs=(),
    )

    assert decision.outcome == "failure"


def test_replay_capture_rejects_candidate_semantic_self_report() -> None:
    payload = {
        "schema_version": "verification-replay-result/v2",
        "run_id": "run-1",
        "cycle": 1,
        "scenario_id": "checkout:case",
        "collection_id": "collection-1",
        "variant": "candidate",
        "input_digest": "a" * 64,
        "plan_digest": "b" * 64,
        "policy_digest": "c" * 64,
        "skill_digests": {"checkout": "d" * 64},
        "source_ref": "candidate-ref",
        "source_digest": "e" * 64,
        "raw_response": {"status_code": 200, "body": {"ok": True}},
        "raw_logs": [],
        "request_id": "request-1",
        "trace_id": "f" * 32,
        "model": "model-a",
        "tool_calls": [],
        "outcome": "success",
        "failure_signatures": [],
        "payload": {"forged": True},
    }

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ReplayResult.model_validate(payload)
