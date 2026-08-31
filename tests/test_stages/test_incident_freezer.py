"""IncidentFreezer: untrusted DiagnosisProposal -> trusted IncidentBundle, fail-closed."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.contracts.diagnosis import (
    DiagnosisProposal,
    ProposedFailureSignature,
    ProposedSourceLocation,
)
from core.contracts.evidence import CorrelationIdentity, PrimarySignal
from core.stages.diagnosis import DiagnosisError, DiagnosisRequest, IncidentFreezer
from core.verification.workflow import ArtifactReference


def _request(tmp_path: Path) -> DiagnosisRequest:
    control = tmp_path / "control"
    control.mkdir()
    (control / "service.py").write_text("line_1\nline_2\nline_3\n", "utf-8")
    artifact = ArtifactReference(uri="file:///evidence/log", sha256="a" * 64)
    return DiagnosisRequest(
        incident_id="incident-1",
        requirement="fix timeout",
        matched_rule="mcp.timeout",
        control_ref="rev-control",
        control_workspace=str(control),
        error_logs=(artifact,),
        original_trace=artifact,
    )


def _proposal(**overrides) -> DiagnosisProposal:
    base = dict(
        symptom_summary="times out",
        source_locations=(
            ProposedSourceLocation(path="service.py", start_line=3, revision="agent-said"),
        ),
        root_cause="deadline reused",
        original_input={"prompt": "x"},
        failure_signature=ProposedFailureSignature(
            code="checkout.timeout", error_type="TimeoutError"
        ),
    )
    base.update(overrides)
    return DiagnosisProposal(**base)


def test_freeze_reanchors_revision_to_trusted_control_ref(tmp_path: Path):
    incident = IncidentFreezer().freeze(_proposal(), request=_request(tmp_path))
    assert incident.incident_id == "incident-1"
    assert incident.matched_rule == "mcp.timeout"
    # the agent's claimed revision is discarded in favor of the trusted control ref
    assert all(loc.revision == "rev-control" for loc in incident.source_locations)
    assert incident.failure_signature.code == "checkout.timeout"
    assert incident.digest  # digest computable


def test_freeze_fails_closed_without_source_location(tmp_path: Path):
    with pytest.raises(DiagnosisError):
        IncidentFreezer().freeze(
            _proposal(source_locations=()), request=_request(tmp_path)
        )


def test_freeze_rejects_source_outside_control(tmp_path: Path):
    outside = ProposedSourceLocation(
        path=str(tmp_path / "elsewhere" / "x.py"), start_line=1, revision="a"
    )
    with pytest.raises(DiagnosisError):
        IncidentFreezer().freeze(
            _proposal(source_locations=(outside,)), request=_request(tmp_path)
        )


def test_freeze_binds_original_input_and_signature_to_primary_signal(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path).model_copy(
        update={
            "primary_signal": PrimarySignal(
                event_id="event-1",
                artifact=ArtifactReference(uri="signal://source#1", sha256="b" * 64),
                observed_at="2026-01-01T00:00:00Z",
                service="orders",
                correlation=CorrelationIdentity(request_id="req-1"),
                error_type="TimeoutError",
                message="checkout timed out",
                original_input={"prompt": "frozen"},
            )
        }
    )
    accepted = IncidentFreezer().freeze(
        _proposal(original_input={"prompt": "frozen"}), request=request
    )
    assert accepted.original_input == {"prompt": "frozen"}

    with pytest.raises(DiagnosisError, match="original_input"):
        IncidentFreezer().freeze(
            _proposal(original_input={"prompt": "easier"}), request=request
        )
    with pytest.raises(DiagnosisError, match="not anchored"):
        IncidentFreezer().freeze(
            _proposal(
                original_input={"prompt": "frozen"},
                failure_signature=ProposedFailureSignature(
                    code="different", error_type="ValueError"
                ),
            ),
            request=request,
        )


def test_freeze_requires_all_comparable_signature_matchers_to_agree(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path).model_copy(
        update={
            "primary_signal": PrimarySignal(
                event_id="event-1",
                artifact=ArtifactReference(uri="signal://source#1", sha256="b" * 64),
                observed_at="2026-01-01T00:00:00Z",
                service="orders",
                error_type="TimeoutError",
                error_code="MCP_TIMEOUT",
                message="checkout timed out after 30s",
                original_input={"prompt": "frozen"},
            )
        }
    )
    proposal = _proposal(
        original_input={"prompt": "frozen"},
        failure_signature=ProposedFailureSignature(
            code="checkout.timeout",
            error_type="ValueError",
            event_code="MCP_TIMEOUT",
            message_pattern="timed out",
        ),
    )

    with pytest.raises(DiagnosisError, match="not anchored"):
        IncidentFreezer().freeze(proposal, request=request)


def test_failure_signature_rejects_empty_matching_pattern() -> None:
    with pytest.raises(ValueError, match="empty string"):
        ProposedFailureSignature(code="timeout", message_pattern=".*")
