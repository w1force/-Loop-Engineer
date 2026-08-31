"""Bounded control-reproduction decisions for diagnosis retries."""

from __future__ import annotations

from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Protocol

from pydantic import Field, ValidationError

from core.contracts.base import Contract
from core.contracts.diagnosis import DiagnosisProposal, DiagnosisReproducerSpec
from core.contracts.evidence import ReproductionAssessment, ReproductionDisposition
from core.contracts.incident import IncidentBundle
from core.state.store import LoopStateStore


class DiagnosisRetryError(RuntimeError):
    """A retry assessment violates hypothesis or attempt binding."""


class DiagnosisRetryAction(str, Enum):
    PROCEED = "proceed"
    REDIAGNOSE = "rediagnose"
    TERMINATE = "terminate"
    ESCALATE = "escalate"


class DiagnosisReproductionRequest(Contract):
    incident: IncidentBundle
    proposal: DiagnosisProposal
    attempt: int = Field(ge=1, le=3)
    control_workspace: str = Field(min_length=1)
    previous_attempts: tuple[ReproductionAssessment, ...] = ()


class ControlReproducer(Protocol):
    async def assess(
        self, request: DiagnosisReproductionRequest
    ) -> ReproductionAssessment: ...


class ReproductionEvidenceStore(Protocol):
    def put_artifact(
        self,
        *,
        artifact_type: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        incident_id: str | None = None,
    ) -> str: ...


class DiagnosisRetryDecision(Contract):
    action: DiagnosisRetryAction
    assessment: ReproductionAssessment
    attempts_remaining: int = Field(ge=0, le=2)


def diagnosis_hypothesis_digest(proposal: DiagnosisProposal) -> str:
    """Trusted identity of the diagnosis hypothesis, excluding narrative noise."""

    payload = {
        "root_cause": proposal.root_cause,
        "source_locations": [
            location.model_dump(mode="json") for location in proposal.source_locations
        ],
        "failure_signature": proposal.failure_signature.model_dump(mode="json"),
        "reproducer": (
            proposal.reproducer.model_dump(mode="json")
            if proposal.reproducer is not None
            else None
        ),
        "original_input": proposal.original_input,
    }
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


class CommandControlReproducer:
    """Execute the frozen diagnosis reproducer in an isolated control copy.

    ``CommandRunner`` owns argv execution, workspace cloning, sandboxing, output
    bounds, timeout and before/after digests.  This adapter only maps that machine
    evidence to the diagnosis retry vocabulary; no Agent-authored verdict is read.
    """

    def __init__(self, *, runner=None, evidence_store: ReproductionEvidenceStore | None = None):
        if runner is None:
            from core.verification.runner import CommandRunner

            runner = CommandRunner()
        self.runner = runner
        self.evidence_store = evidence_store

    async def assess(
        self, request: DiagnosisReproductionRequest
    ) -> ReproductionAssessment:
        hypothesis_digest = diagnosis_hypothesis_digest(request.proposal)
        try:
            proposed = request.proposal.reproducer
            if proposed is None or request.incident.diagnosis_reproducer is None:
                raise ValueError("diagnosis did not provide a control reproducer")
            spec = DiagnosisReproducerSpec.model_validate(
                request.incident.diagnosis_reproducer
            )
            frozen_digest = _json_digest(spec.model_dump(mode="json"))
            if frozen_digest != request.incident.diagnosis_reproducer_digest:
                raise ValueError("frozen diagnosis reproducer digest mismatch")
            if proposed.model_dump(mode="json") != spec.model_dump(mode="json"):
                raise ValueError("diagnosis proposal changed after incident freeze")
        except (ValidationError, ValueError, TypeError) as exc:
            return ReproductionAssessment(
                attempt=request.attempt,
                disposition=ReproductionDisposition.INVALID_REPRODUCER,
                summary=f"invalid control reproducer: {exc}",
                hypothesis_digest=hypothesis_digest,
            )

        # Lazy imports avoid pulling the full verification package into contract
        # module initialization.
        from core.verification.models import CommandSpec, GateKind, Variant

        command = CommandSpec(
            id=spec.id,
            argv=spec.argv,
            cwd=spec.cwd,
            timeout_ms=spec.timeout_ms,
            expected_exit_code=spec.expected_exit_code,
            stdout_contains=spec.stdout_contains,
            stderr_contains=spec.stderr_contains,
        )
        policy_digest = _json_digest(
            {
                "contract": "diagnosis-control-reproduction/v1",
                "incident_digest": request.incident.digest,
                "reproducer_digest": frozen_digest,
            }
        )
        try:
            evidence = await self.runner.run(
                command,
                run_id="diagnosis-" + sha256(
                    request.incident.incident_id.encode("utf-8")
                ).hexdigest()[:24],
                cycle=request.attempt,
                gate=GateKind.BEHAVIOR_COMPARE,
                workspace=request.control_workspace,
                candidate_ref=request.incident.control_ref,
                policy_digest=policy_digest,
                scenario_id=spec.id,
                variant=Variant.CONTROL,
            )
        except Exception as exc:  # trusted runner failure, not a diagnosis failure
            return ReproductionAssessment(
                attempt=request.attempt,
                disposition=ReproductionDisposition.ENVIRONMENT_BLOCKED,
                summary=f"control reproduction runner failed: {type(exc).__name__}: {exc}",
                hypothesis_digest=hypothesis_digest,
            )

        payload = evidence.model_dump(mode="json")
        if self.evidence_store is not None:
            digest = self.evidence_store.put_artifact(
                artifact_type="diagnosis-control-reproduction",
                payload=payload,
                incident_id=request.incident.incident_id,
            )
            evidence_ref = f"artifact://sha256/{digest}"
        else:
            evidence_ref = "sha256:" + _json_digest(payload)

        incomplete = bool(
            evidence.error
            or evidence.timed_out
            or evidence.stdout_truncated
            or evidence.stderr_truncated
            or evidence.stdout_encoding != "utf-8"
            or evidence.stderr_encoding != "utf-8"
        )
        if incomplete:
            disposition = ReproductionDisposition.ENVIRONMENT_BLOCKED
            summary = "control reproduction evidence is incomplete: " + "; ".join(
                evidence.failures
            )
        else:
            combined = evidence.stdout + "\n" + evidence.stderr
            signature_matches = _failure_signature_matches(
                request.incident.failure_signature, combined
            )
            if evidence.passed and signature_matches:
                disposition = ReproductionDisposition.REPRODUCED
                summary = "control reproduced the frozen failure signature"
            else:
                disposition = ReproductionDisposition.NON_REPRODUCIBLE
                details = "; ".join(evidence.failures) or "failure signature absent"
                summary = "control did not reproduce the frozen contract: " + details
        return ReproductionAssessment(
            attempt=request.attempt,
            disposition=disposition,
            summary=summary,
            evidence_refs=(evidence_ref,),
            hypothesis_digest=hypothesis_digest,
        )


def _json_digest(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _failure_signature_matches(signature, output: str) -> bool:
    checks: list[bool] = []
    folded = output.casefold()
    if signature.error_type:
        checks.append(signature.error_type.casefold() in folded)
    if signature.event_code:
        checks.append(signature.event_code.casefold() in folded)
    if signature.message_pattern:
        checks.append(re.search(signature.message_pattern, output) is not None)
    return bool(checks) and all(checks)


class DiagnosisRetryController:
    """Persist only distinct attempts and enforce the three-attempt budget."""

    _TERMINAL_NO_ACTION = {
        ReproductionDisposition.DUPLICATE,
        ReproductionDisposition.STALE_SIGNAL,
        ReproductionDisposition.OLD_VERSION_SIGNAL,
    }

    def __init__(
        self,
        *,
        state_store: LoopStateStore | None = None,
        max_attempts: int = 3,
    ):
        if not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be in 1..3")
        self.state_store = state_store
        self.max_attempts = max_attempts
        self._seen: dict[str, set[str]] = {}

    def record_and_decide(
        self,
        *,
        incident_id: str,
        proposal: DiagnosisProposal,
        assessment: ReproductionAssessment,
    ) -> DiagnosisRetryDecision:
        expected_digest = diagnosis_hypothesis_digest(proposal)
        if assessment.hypothesis_digest != expected_digest:
            raise DiagnosisRetryError(
                "reproduction assessment is bound to another hypothesis"
            )
        seen = self._seen.setdefault(incident_id, set())
        persisted = (
            self.state_store.list_diagnosis_attempts(incident_id)
            if self.state_store is not None
            else []
        )
        seen.update(item["hypothesis_digest"] for item in persisted)
        expected_attempt = len(persisted) + 1 if self.state_store is not None else len(seen) + 1
        if (
            assessment.attempt != expected_attempt
            or assessment.attempt > self.max_attempts
        ):
            raise DiagnosisRetryError(
                f"expected diagnosis attempt {expected_attempt}, got {assessment.attempt}"
            )
        if expected_digest in seen:
            raise DiagnosisRetryError("re-diagnosis repeated an earlier hypothesis")
        if self.state_store is not None:
            self.state_store.record_diagnosis_attempt(
                incident_id=incident_id,
                attempt=assessment.attempt,
                hypothesis_digest=assessment.hypothesis_digest,
                disposition=assessment.disposition.value,
                summary=assessment.summary,
                evidence_refs=assessment.evidence_refs,
            )
        seen.add(expected_digest)

        if assessment.disposition is ReproductionDisposition.REPRODUCED:
            action = DiagnosisRetryAction.PROCEED
        elif assessment.disposition in self._TERMINAL_NO_ACTION:
            action = DiagnosisRetryAction.TERMINATE
        elif assessment.attempt >= self.max_attempts:
            action = DiagnosisRetryAction.ESCALATE
        else:
            action = DiagnosisRetryAction.REDIAGNOSE
        return DiagnosisRetryDecision(
            action=action,
            assessment=assessment,
            attempts_remaining=max(0, self.max_attempts - assessment.attempt),
        )


__all__ = [
    "ControlReproducer",
    "CommandControlReproducer",
    "DiagnosisReproductionRequest",
    "DiagnosisRetryAction",
    "DiagnosisRetryController",
    "DiagnosisRetryDecision",
    "DiagnosisRetryError",
    "diagnosis_hypothesis_digest",
]
