"""IncidentFreezer: untrusted DiagnosisProposal -> trusted IncidentBundle, fail-closed."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.contracts.diagnosis import (
    DiagnosisProposal,
    ProposedFailureSignature,
    ProposedSourceLocation,
)
from core.stages.diagnosis import DiagnosisError, DiagnosisRequest, IncidentFreezer
from core.verification.workflow import ArtifactReference


def _request(tmp_path: Path) -> DiagnosisRequest:
    control = tmp_path / "control"
    control.mkdir()
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
