"""LoopEngineer orchestration wiring: diagnosis -> freeze -> repair/verify loop.

Uses a real IncidentFreezer and stubs the diagnosis agent and coordinator so the
test exercises orchestration wiring (stage order, incident freezing, RepairStage
adapter feedback conversion) without the heavy Docker/engine machinery, which is
covered by tests/test_verification_skill/test_coordinator.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.contracts.diagnosis import DiagnosisProposal, ProposedFailureSignature, ProposedSourceLocation
from core.contracts.repair import RepairCycleRequest, RepairResult
from core.orchestrator.loop_engineer import LoopEngineer, LoopRunRequest, _RepairStageAgent
from core.stages.diagnosis import DiagnosisRequest, IncidentFreezer
from core.verification.coordinator import CoordinatorOutcome, CoordinatorStatus
from core.verification.workflow import ArtifactReference


def _diagnosis_request(tmp_path: Path) -> DiagnosisRequest:
    control = tmp_path / "control"
    control.mkdir()
    (control / "service.py").write_text("x = 1\n", encoding="utf-8")
    artifact = ArtifactReference(uri="file:///evidence/log", sha256="a" * 64)
    return DiagnosisRequest(
        incident_id="incident-1",
        requirement="fix the checkout timeout",
        matched_rule="mcp.timeout.no_fallback",
        control_ref="rev-control",
        control_workspace=str(control),
        error_logs=(artifact,),
        original_trace=artifact,
    )


def _proposal() -> DiagnosisProposal:
    return DiagnosisProposal(
        symptom_summary="checkout times out with no fallback",
        affected_components=("checkout",),
        risk_tags=("timeout",),
        hypotheses=("missing fallback",),
        confirmed_facts=("trace shows TimeoutError",),
        source_locations=(
            ProposedSourceLocation(path="service.py", start_line=1, revision="ignored"),
        ),
        root_cause="deadline reused across retries",
        original_input={"prompt": "checkout"},
        failure_signature=ProposedFailureSignature(
            code="checkout.timeout", error_type="TimeoutError"
        ),
    )


class _StubDiagnosisStage:
    def __init__(self, proposal):
        self._proposal = proposal
        self.calls = 0

    async def run(self, request, **_):
        self.calls += 1
        return self._proposal


class _StubCoordinator:
    def __init__(self):
        self.calls = []

    async def run(self, request, **adapters):
        self.calls.append((request, adapters))
        return CoordinatorOutcome(
            run_id=request.run_id,
            incident_id=request.incident.incident_id,
            status=CoordinatorStatus.VERIFIED,
            cycle=1,
            evidence_location="file:///evidence/report",
        )


@pytest.mark.asyncio
async def test_loop_engineer_runs_diagnosis_then_coordinator(tmp_path: Path) -> None:
    diagnosis = _StubDiagnosisStage(_proposal())
    coordinator = _StubCoordinator()
    engine = LoopEngineer(
        diagnosis_stage=diagnosis,
        incident_freezer=IncidentFreezer(),
        repair_stage=object(),  # unused: stub coordinator never calls repair_agent
        coordinator=coordinator,
    )
    request = LoopRunRequest(
        run_id="run-1",
        diagnosis=_diagnosis_request(tmp_path),
        candidate_workspace=str(tmp_path / "candidate"),
    )
    (tmp_path / "candidate").mkdir()

    outcome = await engine.run(
        request,
        parent_agent_state=object(),
        parent_params=object(),
        tracer=object(),
        lightweight_verifier=object(),
        planner=object(),
    )

    assert diagnosis.calls == 1
    assert len(coordinator.calls) == 1
    coord_request, adapters = coordinator.calls[0]
    # Diagnosis fed a trusted, frozen incident into the loop.
    assert coord_request.incident.incident_id == "incident-1"
    assert coord_request.incident.matched_rule == "mcp.timeout.no_fallback"
    # Source location revision is re-anchored to the trusted control ref, not the agent's.
    assert coord_request.incident.source_locations[0].revision == "rev-control"
    assert coord_request.incident.root_cause == "deadline reused across retries"
    # Repair is wired through the RepairStage adapter, not FreshContextRepairAgent.
    assert isinstance(adapters["repair_agent"], _RepairStageAgent)
    assert outcome.verified is True
    assert outcome.incident_digest == coord_request.incident.digest


@pytest.mark.asyncio
async def test_repair_stage_adapter_converts_prior_failures_to_findings(
    tmp_path: Path,
) -> None:
    captured = {}

    class _RecordingStage:
        async def repair(self, request, *, parent_agent_state, parent_params, tracer, feedback):
            captured["feedback"] = feedback
            return RepairResult(
                workspace=request.candidate_workspace,
                candidate_ref="candidate:x",
                implementation_summary="added fallback",
                test_entrypoints=("pytest tests/test_x.py",),
            )

    adapter = _RepairStageAgent(
        stage=_RecordingStage(),
        parent_agent_state=object(),
        parent_params=object(),
        tracer=object(),
    )
    incident = _make_incident(tmp_path)
    result = await adapter.repair(
        RepairCycleRequest(
            run_id="run-1",
            cycle=2,
            incident=incident,
            candidate_workspace=str(tmp_path / "candidate"),
            previous_failures=("gate unit: assertion failed",),
        )
    )
    feedback = captured["feedback"]
    assert feedback.cycle == 2
    assert len(feedback.findings) == 1
    assert feedback.findings[0].owner.value == "repair"
    assert feedback.findings[0].summary == "gate unit: assertion failed"
    assert result.implementation_summary == "added fallback"


def _make_incident(tmp_path: Path):
    request = _diagnosis_request(tmp_path)
    return IncidentFreezer().freeze(_proposal(), request=request)
