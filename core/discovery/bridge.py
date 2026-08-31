"""Bridge a deduped incident into a DiagnosisRequest for the LoopEngineer.

Discovery produces incidents; the orchestrator consumes a DiagnosisRequest. This is
the artifact hand-off between the two. The control ref + workspace come from the
application registry (trusted config), not from the signal. OTLP trace linkage is
attached later in verification; here the run-log line is the evidence pointer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.contracts.evidence import CorrelationIdentity, IncidentSignal, PrimarySignal
from core.state.store import IncidentRecord, content_digest


def _timestamp_ns(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1_000_000_000)
    except ValueError:
        return None


def _artifact_for_signal(sample: dict[str, Any]):
    from core.contracts.incident import ArtifactReference

    source_id = sample.get("source_id", "unknown")
    evidence = sample.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    line_offset = evidence.get("line_offset", 0)
    generation = evidence.get("source_generation")
    generation_suffix = f"@{generation}" if generation else ""
    return ArtifactReference(
        uri=f"signal://{source_id}{generation_suffix}#{line_offset}",
        sha256=content_digest(sample),
        media_type="application/x-loop-signal+json",
    )


def _signal_fields(
    sample: dict[str, Any],
    *,
    artifact,
    event_id: str,
    service: str,
    include_original_input: bool = True,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "artifact": artifact,
        "observed_at": sample.get("observed_at") or "unknown",
        "timestamp_ns": _timestamp_ns(sample.get("observed_at")),
        "service": sample.get("service") or service,
        "environment": sample.get("environment"),
        "deployment_version": sample.get("deployment_version"),
        "instance_id": sample.get("instance_id"),
        "thread_id": sample.get("thread_id"),
        "task_id": sample.get("task_id"),
        "logger": sample.get("logger"),
        "correlation": CorrelationIdentity(
            trace_id=sample.get("trace_id"),
            request_id=sample.get("request_id"),
            run_id=sample.get("run_id"),
            session_id=sample.get("session_id"),
            erp=sample.get("erp"),
        ),
        "event_name": sample.get("event_name") or sample.get("kind"),
        "error_type": sample.get("error_type"),
        "error_code": sample.get("error_code"),
        "message": sample.get("message") or "",
        "message_template": sample.get("message_template"),
        "template_id": sample.get("template_id"),
        # The immutable primary owns the reproduction input. Repeating arbitrary
        # secondary inputs in the Agent prompt creates an unbounded context vector.
        "original_input": sample.get("original_input") if include_original_input else None,
    }


def incident_to_diagnosis_request(
    incident: "IncidentRecord | dict[str, Any]",
    *,
    control_workspace: str,
    control_ref: str,
    requirement: str | None = None,
):
    from core.stages.diagnosis import DiagnosisRequest

    if isinstance(incident, IncidentRecord):
        incident_id = incident.incident_id
        matched_rule = incident.matched_rule
        sample = incident.sample
        related = incident.signals
        service = incident.service
    else:
        incident_id = incident["incident_id"]
        matched_rule = incident["matched_rule"]
        sample = incident["sample"]
        related = tuple(incident.get("signals", ()))
        service = incident.get("service", "unknown")

    log_ref = _artifact_for_signal(sample)
    log_refs = [log_ref]
    related_signals: list[IncidentSignal] = []
    for relation in related:
        if len(related_signals) >= 200:
            break
        payload = relation.get("payload") or {}
        ref = _artifact_for_signal(payload)
        if ref.sha256 != log_ref.sha256:
            log_refs.append(ref)
            related_signals.append(
                IncidentSignal(
                    **_signal_fields(
                        payload,
                        artifact=ref,
                        event_id=payload.get("signal_id")
                        or relation.get("signal_id")
                        or f"{incident_id}:secondary:{len(related_signals) + 1}",
                        service=service,
                        include_original_input=False,
                    ),
                    correlation_reason=relation.get("correlation_reason") or "incident",
                )
            )

    primary_signal = PrimarySignal(
        **_signal_fields(
            sample,
            artifact=log_ref,
            event_id=sample.get("signal_id") or f"{incident_id}:primary",
            service=service,
        )
    )
    default_requirement = (
        f"Diagnose and repair rule '{matched_rule}': "
        + (sample.get("message") or sample.get("error_type") or "")[:200]
    ).strip()

    return DiagnosisRequest(
        incident_id=incident_id,
        requirement=requirement or default_requirement or f"Diagnose {matched_rule}",
        matched_rule=matched_rule,
        control_ref=control_ref,
        control_workspace=control_workspace,
        error_logs=tuple(log_refs),
        # No OTLP trace at discovery time; the run-log line is the evidence pointer.
        # Verification attaches the real trace window.
        original_trace=log_ref,
        primary_signal=primary_signal,
        related_signals=tuple(related_signals),
    )


__all__ = ["incident_to_diagnosis_request"]
