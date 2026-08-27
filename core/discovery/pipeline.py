"""Discovery pipeline: incremental scan -> detect -> dedup -> persist incidents.

Ties the log connector, the versioned detector and the durable state store into one
step. Cron/automation only calls ``scan()``; it does not invoke any agent
(PRD §16 "Cron 只负责入队，不直接调用 Agent").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from core.connectors.logs import JsonlRunLogConnector, LogRecord
from core.state.store import IncidentRecord, LoopStateStore, content_digest

from .contracts import SignalEnvelope
from .detector import DEFAULT_RULESET, RuleSet
from .fingerprint import compute_fingerprint


def _tool(record: LogRecord) -> str | None:
    payload = record.payload
    for key in ("tool", "tool_name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@dataclass(frozen=True)
class DiscoveryResult:
    records_scanned: int
    signals_recorded: int
    new_incidents: tuple[IncidentRecord, ...]
    updated_incidents: tuple[IncidentRecord, ...]


class DiscoveryPipeline:
    def __init__(
        self,
        state_store: LoopStateStore,
        *,
        ruleset: RuleSet = DEFAULT_RULESET,
    ):
        self.state = state_store
        self.ruleset = ruleset

    def scan(
        self,
        connector: JsonlRunLogConnector,
        path,
        *,
        service: str,
        suppression_seconds: int = 3600,
    ) -> DiscoveryResult:
        records = connector.read_new(path)
        signals_recorded = 0
        new_incidents: list[IncidentRecord] = []
        updated_incidents: list[IncidentRecord] = []

        for record in records:
            detection = self.ruleset.detect(record)
            if detection is None:
                continue
            tool = _tool(record)
            fingerprint = compute_fingerprint(
                service=service, detection=detection, tool=tool
            )
            observed_at = record.ts or datetime.now(timezone.utc).isoformat()
            envelope = SignalEnvelope(
                signal_id=content_digest(
                    {
                        "source": record.source_id,
                        "offset": record.line_offset,
                        "fp": fingerprint,
                    }
                ),
                fingerprint=fingerprint,
                service=service,
                source_id=record.source_id,
                kind=record.kind,
                observed_at=observed_at,
                matched_rule=detection.matched_rule,
                rule_version=detection.rule_version,
                severity=detection.severity,
                eligibility=detection.eligibility,
                error_type=detection.error_type,
                message=detection.message,
                original_input=record.payload.get("original_input")
                or record.payload.get("input"),
                evidence={
                    "line_offset": record.line_offset,
                    "seq": record.seq,
                    "chain_id": record.chain_id,
                    "turn": record.turn,
                },
            )

            incident = self.state.upsert_incident(
                fingerprint=fingerprint,
                matched_rule=detection.matched_rule,
                severity=detection.severity.value,
                eligibility=detection.eligibility.value,
                service=service,
                sample=envelope.model_dump(mode="json"),
                suppression_seconds=suppression_seconds,
            )
            inserted = self.state.record_signal(
                signal_id=envelope.signal_id,
                fingerprint=fingerprint,
                source_id=record.source_id,
                kind=record.kind,
                observed_at=observed_at,
                payload=envelope.model_dump(mode="json"),
                incident_id=incident.incident_id,
            )
            signals_recorded += int(inserted)
            if incident.created:
                new_incidents.append(incident)
            else:
                updated_incidents.append(incident)

        return DiscoveryResult(
            records_scanned=len(records),
            signals_recorded=signals_recorded,
            new_incidents=tuple(new_incidents),
            updated_incidents=tuple(updated_incidents),
        )


__all__ = ["DiscoveryPipeline", "DiscoveryResult"]
