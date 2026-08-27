"""Bridge a deduped incident into a DiagnosisRequest for the LoopEngineer.

Discovery produces incidents; the orchestrator consumes a DiagnosisRequest. This is
the artifact hand-off between the two. The control ref + workspace come from the
application registry (trusted config), not from the signal. OTLP trace linkage is
attached later in verification; here the run-log line is the evidence pointer.
"""

from __future__ import annotations

from typing import Any

from core.state.store import IncidentRecord, content_digest


def incident_to_diagnosis_request(
    incident: "IncidentRecord | dict[str, Any]",
    *,
    control_workspace: str,
    control_ref: str,
    requirement: str | None = None,
):
    from core.contracts.incident import ArtifactReference
    from core.stages.diagnosis import DiagnosisRequest

    if isinstance(incident, IncidentRecord):
        incident_id = incident.incident_id
        matched_rule = incident.matched_rule
        sample = incident.sample
    else:
        incident_id = incident["incident_id"]
        matched_rule = incident["matched_rule"]
        sample = incident["sample"]

    digest = content_digest(sample)
    source_id = sample.get("source_id", "unknown")
    line_offset = sample.get("evidence", {}).get("line_offset", 0)
    log_ref = ArtifactReference(
        uri=f"signal://{source_id}#{line_offset}",
        sha256=digest,
        media_type="application/x-loop-signal+json",
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
        error_logs=(log_ref,),
        # No OTLP trace at discovery time; the run-log line is the evidence pointer.
        # Verification attaches the real trace window.
        original_trace=log_ref,
    )


__all__ = ["incident_to_diagnosis_request"]
