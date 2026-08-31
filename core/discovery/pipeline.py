"""Discovery pipeline: incremental scan -> detect -> dedup -> persist incidents.

Ties the log connector, the versioned detector and the durable state store into one
step. Cron/automation only calls ``scan()``; it does not invoke any agent
(PRD §16 "Cron 只负责入队，不直接调用 Agent").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from core.connectors.logs import JsonlRunLogConnector, LogRecord
from core.state.store import IncidentRecord, LoopStateStore, content_digest

from .contracts import SignalEnvelope
from .detector import DEFAULT_RULESET, RuleSet
from .fingerprint import compute_fingerprint, normalize_message


def _tool(record: LogRecord) -> str | None:
    payload = record.payload
    for key in ("tool", "tool_name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _text(record: LogRecord, *keys: str) -> str | None:
    for source in (record.payload, record.raw):
        for key in keys:
            value = source.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def _trace_id(record: LogRecord) -> str | None:
    value = _text(record, "trace_id", "traceId", "traceid", "log_traceid", "logTraceId")
    if value is not None and re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return value.lower()
    return value


def _original_input(record: LogRecord):
    for key in ("original_input", "input"):
        if key in record.payload:
            return record.payload[key]
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
            raw_digest = content_digest(record.raw)
            signal_id = content_digest(
                {
                    "source": record.source_id,
                    "generation": record.source_generation,
                    "offset": record.line_offset,
                    "raw_digest": raw_digest,
                }
            )
            message_template = normalize_message(detection.message)
            envelope = SignalEnvelope(
                signal_id=signal_id,
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
                error_code=_text(record, "error_code", "event_code", "code"),
                event_name=_text(record, "event_name", "event.name", "eventName")
                or record.kind,
                message=detection.message,
                message_template=message_template,
                template_id=content_digest(
                    {
                        "service": service,
                        "event_name": _text(
                            record, "event_name", "event.name", "eventName"
                        )
                        or record.kind,
                        "template": message_template,
                    }
                ),
                environment=_text(record, "environment", "env"),
                deployment_version=_text(
                    record, "deployment_version", "service_version", "version"
                ),
                instance_id=_text(record, "instance_id", "pod", "pod_name"),
                thread_id=_text(record, "thread_id", "thread"),
                task_id=_text(record, "task_id", "task"),
                logger=_text(record, "logger", "logger_name"),
                trace_id=_trace_id(record),
                request_id=_text(
                    record, "request_id", "requestId", "requestid", "log_requestid"
                ),
                run_id=_text(record, "run_id", "runId"),
                session_id=_text(record, "session_id", "sessionId"),
                erp=_text(record, "erp", "ERP", "user", "user_id"),
                original_input=_original_input(record),
                evidence={
                    "line_offset": record.line_offset,
                    "source_generation": record.source_generation,
                    "raw_sha256": raw_digest,
                    "seq": record.seq,
                    "chain_id": record.chain_id,
                    "turn": record.turn,
                },
            )

            ingested = self.state.ingest_signal(
                signal_id=envelope.signal_id,
                fingerprint=fingerprint,
                source_id=record.source_id,
                kind=record.kind,
                observed_at=observed_at,
                payload=envelope.model_dump(mode="json"),
                matched_rule=detection.matched_rule,
                severity=detection.severity.value,
                eligibility=detection.eligibility.value,
                service=service,
                trace_id=envelope.trace_id,
                request_id=envelope.request_id,
                run_id=envelope.run_id,
                session_id=envelope.session_id,
                erp=envelope.erp,
                environment=envelope.environment,
                deployment_version=envelope.deployment_version,
                suppression_seconds=suppression_seconds,
            )
            signals_recorded += int(ingested.signal_created)
            if not ingested.signal_created:
                continue
            if ingested.incident.created:
                new_incidents.append(ingested.incident)
            else:
                updated_incidents.append(ingested.incident)

        def refreshed(
            records: list[IncidentRecord], *, created: bool
        ) -> tuple[IncidentRecord, ...]:
            unique: dict[str, IncidentRecord] = {}
            for record in records:
                current = self.state.get_incident_record(
                    record.incident_id, created=created
                )
                if current is not None:
                    unique[record.incident_id] = current
            return tuple(unique.values())

        return DiscoveryResult(
            records_scanned=len(records),
            signals_recorded=signals_recorded,
            new_incidents=refreshed(new_incidents, created=True),
            updated_incidents=refreshed(updated_incidents, created=False),
        )


__all__ = ["DiscoveryPipeline", "DiscoveryResult"]
