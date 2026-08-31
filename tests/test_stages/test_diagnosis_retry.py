"""Diagnosis retry budget counts distinct hypotheses, not repair attempts."""

from __future__ import annotations

from hashlib import sha256
import json

import pytest

from core.contracts.diagnosis import (
    DiagnosisProposal,
    DiagnosisReproducerSpec,
    ProposedFailureSignature,
    ProposedSourceLocation,
)
from core.contracts.evidence import ReproductionAssessment, ReproductionDisposition
from core.stages.diagnosis import (
    CommandControlReproducer,
    DiagnosisReproductionRequest,
    DiagnosisRetryAction,
    DiagnosisRetryController,
    DiagnosisRetryError,
    diagnosis_hypothesis_digest,
)
from core.verification.models import (
    CommandEvidence,
    GateKind,
    Variant,
    command_contract_digest,
)
from core.verification.workflow import ArtifactReference, IncidentBundle, SourceLocation


def _proposal(cause: str) -> DiagnosisProposal:
    return DiagnosisProposal(
        symptom_summary="timeout",
        source_locations=(
            ProposedSourceLocation(path="service.py", start_line=1, revision="control"),
        ),
        root_cause=cause,
        reproducer={"argv": ["pytest", "test_timeout.py"]},
        original_input={"id": 1},
        failure_signature=ProposedFailureSignature(
            code="timeout", error_type="TimeoutError"
        ),
    )


def _assessment(
    proposal: DiagnosisProposal,
    attempt: int,
    disposition: ReproductionDisposition = ReproductionDisposition.NON_REPRODUCIBLE,
) -> ReproductionAssessment:
    return ReproductionAssessment(
        attempt=attempt,
        disposition=disposition,
        summary="control result",
        hypothesis_digest=diagnosis_hypothesis_digest(proposal),
    )


def test_three_distinct_non_reproductions_escalate_only_on_third() -> None:
    controller = DiagnosisRetryController(max_attempts=3)
    actions = []
    for attempt in range(1, 4):
        proposal = _proposal(f"cause-{attempt}")
        actions.append(
            controller.record_and_decide(
                incident_id="incident-1",
                proposal=proposal,
                assessment=_assessment(proposal, attempt),
            ).action
        )
    assert actions == [
        DiagnosisRetryAction.REDIAGNOSE,
        DiagnosisRetryAction.REDIAGNOSE,
        DiagnosisRetryAction.ESCALATE,
    ]


def test_repeated_hypothesis_is_rejected_without_consuming_another_attempt() -> None:
    controller = DiagnosisRetryController(max_attempts=3)
    proposal = _proposal("same-cause")
    controller.record_and_decide(
        incident_id="incident-1",
        proposal=proposal,
        assessment=_assessment(proposal, 1),
    )
    with pytest.raises(DiagnosisRetryError, match="repeated"):
        controller.record_and_decide(
            incident_id="incident-1",
            proposal=proposal,
            assessment=_assessment(proposal, 2),
        )


@pytest.mark.parametrize(
    "disposition",
    [
        ReproductionDisposition.DUPLICATE,
        ReproductionDisposition.STALE_SIGNAL,
        ReproductionDisposition.OLD_VERSION_SIGNAL,
    ],
)
def test_processed_or_prefixed_signal_terminates_without_retry(disposition) -> None:
    controller = DiagnosisRetryController()
    proposal = _proposal("already fixed")
    decision = controller.record_and_decide(
        incident_id="incident-1",
        proposal=proposal,
        assessment=_assessment(proposal, 1, disposition),
    )
    assert decision.action is DiagnosisRetryAction.TERMINATE


def test_reproduced_control_proceeds_to_repair() -> None:
    controller = DiagnosisRetryController()
    proposal = _proposal("real cause")
    decision = controller.record_and_decide(
        incident_id="incident-1",
        proposal=proposal,
        assessment=_assessment(proposal, 1, ReproductionDisposition.REPRODUCED),
    )
    assert decision.action is DiagnosisRetryAction.PROCEED


@pytest.mark.parametrize(
    "argv",
    [
        ("python", "-cprint('x')"),
        ("node", "--eval=console.log(1)"),
        ("uv", "run", "python", "-c", "print('x')"),
        ("npx", "node", "-econsole.log(1)"),
        ("npm", "exec", "--call=echo unsafe"),
        ("uv", "run", "bash", "test.sh"),
    ],
)
def test_reproducer_rejects_inline_and_shell_wrappers(argv) -> None:
    with pytest.raises(ValueError):
        DiagnosisReproducerSpec(argv=argv)


def test_reproducer_accepts_named_repository_test_command() -> None:
    spec = DiagnosisReproducerSpec(
        argv=("uv", "run", "pytest", "tests/test_timeout.py")
    )
    assert spec.argv[-1] == "tests/test_timeout.py"


@pytest.mark.asyncio
async def test_command_control_reproducer_maps_command_evidence() -> None:
    proposal = _proposal("timeout budget reused").model_copy(
        update={
            "failure_signature": ProposedFailureSignature(
                code="timeout",
                error_type="TimeoutError",
                event_code="MCP_TIMEOUT",
            )
        }
    )
    reproducer_payload = proposal.reproducer.model_dump(mode="json")
    incident = IncidentBundle(
        incident_id="incident-1",
        requirement="reproduce timeout",
        matched_rule="service.timeout",
        error_logs=(ArtifactReference(uri="signal://source#1", sha256="a" * 64),),
        original_trace=ArtifactReference(uri="signal://source#1", sha256="a" * 64),
        source_locations=(
            SourceLocation(path="service.py", start_line=1, revision="control"),
        ),
        root_cause=proposal.root_cause,
        control_ref="control",
        original_input=proposal.original_input,
        failure_signature={
            "code": "timeout",
            "error_type": "TimeoutError",
            "event_code": "MCP_TIMEOUT",
        },
        diagnosis_reproducer=reproducer_payload,
        diagnosis_reproducer_digest=sha256(
            json.dumps(
                reproducer_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
    )

    class Runner:
        def __init__(self, stdout: str):
            self.stdout = stdout

        async def run(self, command, **kwargs):
            stdout = self.stdout
            return CommandEvidence(
                run_id=kwargs["run_id"],
                cycle=kwargs["cycle"],
                gate=GateKind.BEHAVIOR_COMPARE,
                check_id=command.id,
                scenario_id=kwargs["scenario_id"],
                variant=Variant.CONTROL,
                policy_digest=kwargs["policy_digest"],
                command_spec_digest=command_contract_digest(command),
                command_spec=command,
                candidate_ref=kwargs["candidate_ref"],
                candidate_digest_before="b" * 64,
                candidate_digest_after="b" * 64,
                argv=command.argv,
                cwd=command.cwd,
                exit_code=0,
                stdout=stdout,
                stderr="",
                stdout_sha256=sha256(stdout.encode("utf-8")).hexdigest(),
                stderr_sha256=sha256(b"").hexdigest(),
                stdout_bytes=len(stdout.encode("utf-8")),
                stderr_bytes=0,
                sandbox_backend="macos-seatbelt+workspace-copy",
                duration_ms=1,
                expected_exit_code=command.expected_exit_code,
                expected_stdout_contains=command.stdout_contains,
                expected_stderr_contains=command.stderr_contains,
                forbidden_output_patterns=command.forbidden_output_patterns,
                passed=True,
            )

    assessment = await CommandControlReproducer(
        runner=Runner("TimeoutError observed")
    ).assess(
        DiagnosisReproductionRequest(
            incident=incident,
            proposal=proposal,
            attempt=1,
            control_workspace="/tmp/control",
        )
    )

    assert assessment.disposition is ReproductionDisposition.NON_REPRODUCIBLE
    assert assessment.evidence_refs[0].startswith("sha256:")

    reproduced = await CommandControlReproducer(
        runner=Runner("TimeoutError MCP_TIMEOUT observed")
    ).assess(
        DiagnosisReproductionRequest(
            incident=incident,
            proposal=proposal,
            attempt=1,
            control_workspace="/tmp/control",
        )
    )
    assert reproduced.disposition is ReproductionDisposition.REPRODUCED
