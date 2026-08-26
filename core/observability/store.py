"""SQLite-backed OTLP trace/log repository for the local CCB service.

CCB already exports OpenTelemetry traces and event logs.  This module accepts
the OTLP/HTTP JSON shape produced by that exporter and keeps the raw payload
alongside indexed correlation fields.  It also imports CCB's human-readable
debug files without pretending that those lines carry a native Trace ID.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Literal


SCHEMA_VERSION = 5
_DEBUG_LINE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\S+)\s+"
    r"\[(?P<level>VERBOSE|DEBUG|INFO|WARN|ERROR)\]\s*(?P<message>.*)$"
)
_ERROR_TYPE = re.compile(
    r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Failure|Timeout))\b"
)
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
_HEX = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_NUMBER = re.compile(r"(?<![A-Za-z_])-?\d+(?:\.\d+)?")


class ObservabilityStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionWindow:
    run_id: str
    cycle: int
    scenario_id: str
    variant: Literal["control", "candidate"]
    input_digest: str
    input_payload: Any
    collection_id: str
    control_ref: str
    control_digest: str
    candidate_ref: str
    candidate_digest: str
    policy_digest: str
    skill_digests: Mapping[str, str]
    started_at_ns: int
    ended_at_ns: int
    collection_complete: bool
    trace_id: str | None = None
    request_id: str | None = None
    session_id: str | None = None
    finished: bool | None = None
    outcome: Literal["success", "failure"] | None = None
    payload: Any = None
    model: str | None = None
    tool_calls: tuple[Mapping[str, Any], ...] | None = None
    oracle_digest: str | None = None
    result_sha256: str | None = None


@dataclass(frozen=True)
class OtlpFlushBarrier:
    """Coordinator-authenticated acknowledgement of an exporter force-flush."""

    flush_id: str
    collection_id: str
    run_id: str
    cycle: int
    scenario_id: str
    variant: Literal["control", "candidate"]
    input_digest: str
    signals: tuple[Literal["traces", "logs"], ...]
    flush_started_at_ns: int
    flush_completed_at_ns: int
    deadline_ns: int


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def normalized_input_digest(value: Any) -> str:
    """Digest the exact JSON scenario input used for control/candidate replay."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ObservabilityStoreError("input_payload 必须是有限 JSON 值") from exc
    return sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decode_any_value(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    for key in (
        "stringValue",
        "boolValue",
        "intValue",
        "doubleValue",
        "bytesValue",
    ):
        if key in value:
            result = value[key]
            if key == "intValue":
                return _integer(result)
            return result
    if "arrayValue" in value:
        array = value.get("arrayValue") or {}
        return [_decode_any_value(item) for item in array.get("values", [])]
    if "kvlistValue" in value:
        mapping = value.get("kvlistValue") or {}
        return _decode_attributes(mapping.get("values", []))
    return dict(value)


def _decode_attributes(items: Any) -> dict[str, Any]:
    if not isinstance(items, list):
        return {}
    result: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("key"), str):
            continue
        result[item["key"]] = _decode_any_value(item.get("value"))
    return result


def _lookup(attributes: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in attributes:
            return attributes[name]
    return None


def _normalize_identifier(value: Any, expected_bytes: int) -> str:
    if value is None:
        return ""
    raw = str(value).strip()
    if re.fullmatch(rf"[0-9a-fA-F]{{{expected_bytes * 2}}}", raw):
        return raw.lower()
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error):
        return ""
    return decoded.hex() if len(decoded) == expected_bytes else ""


def _message_template(message: str) -> str:
    normalized = _UUID.sub("<uuid>", message)
    normalized = _HEX.sub("<hex>", normalized)
    normalized = _NUMBER.sub("<n>", normalized)
    return re.sub(r"\s+", " ", normalized).strip()[:1000]


def _error_type(message: str, attributes: Mapping[str, Any]) -> str:
    explicit = _lookup(
        attributes,
        "error.type",
        "exception.type",
        "error_type",
        "errorType",
    )
    if explicit:
        return str(explicit)[:255]
    match = _ERROR_TYPE.search(message)
    return match.group(1) if match else "UnclassifiedError"


class LocalObservabilityStore:
    """Durable local evidence store; every public operation uses a transaction."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _next_ingest_sequence(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT sequence FROM otlp_ingest_clock WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise ObservabilityStoreError("OTLP ingest clock 未初始化")
        sequence = int(row["sequence"]) + 1
        connection.execute(
            "UPDATE otlp_ingest_clock SET sequence = ? WHERE singleton = 1",
            (sequence,),
        )
        return sequence

    @staticmethod
    def _reject_if_late(
        connection: sqlite3.Connection,
        *,
        resource: Mapping[str, Any],
        trace_id: str | None,
    ) -> bool:
        """Seal verification evidence at the barrier and durably mark late writes."""

        run_id = _nullable(
            _lookup(resource, "verification.run_id", "verification_run_id")
        )
        scenario_id = _nullable(
            _lookup(resource, "verification.scenario_id", "scenario_id")
        )
        cycle = _positive_integer_or_none(
            _lookup(resource, "verification.cycle", "verification_cycle")
        )
        variant = _nullable(_lookup(resource, "verification.variant", "variant"))
        input_digest = _nullable(
            _lookup(resource, "verification.input_digest", "input_digest")
        )
        collection_id = _nullable(
            _lookup(
                resource,
                "verification.collection_id",
                "verification_collection_id",
            )
        )
        matches: dict[str, sqlite3.Row] = {}
        if collection_id:
            rows = connection.execute(
                "SELECT * FROM otlp_flush_barriers WHERE collection_id = ?",
                (collection_id,),
            ).fetchall()
            matches.update({row["flush_id"]: row for row in rows})
        elif all((run_id, cycle, scenario_id, variant, input_digest)):
            rows = connection.execute(
                """
                SELECT * FROM otlp_flush_barriers
                WHERE run_id = ? AND cycle = ? AND scenario_id = ? AND variant = ?
                  AND input_digest = ?
                """,
                (run_id, cycle, scenario_id, variant, input_digest),
            ).fetchall()
            matches.update({row["flush_id"]: row for row in rows})
        if trace_id:
            rows = connection.execute(
                """
                SELECT barrier.*
                FROM otlp_flush_barriers AS barrier
                JOIN execution_windows AS window
                  ON window.collection_id = barrier.collection_id
                WHERE window.trace_id = ?
                """,
                (trace_id,),
            ).fetchall()
            matches.update({row["flush_id"]: row for row in rows})
        if not matches:
            return False
        now_ns = time.time_ns()
        for flush_id in matches:
            connection.execute(
                """
                UPDATE otlp_flush_barriers
                SET late_arrival_count = late_arrival_count + 1,
                    last_late_at_ns = ?
                WHERE flush_id = ?
                """,
                (now_ns, flush_id),
            )
        return True

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trace_spans (
                    trace_id TEXT NOT NULL,
                    span_id TEXT NOT NULL,
                    parent_span_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    start_time_ns INTEGER NOT NULL,
                    end_time_ns INTEGER NOT NULL,
                    status_code TEXT,
                    status_message TEXT,
                    service_name TEXT,
                    service_version TEXT,
                    session_id TEXT,
                    run_id TEXT,
                    cycle INTEGER,
                    scenario_id TEXT,
                    variant TEXT,
                    input_digest TEXT,
                    collection_id TEXT,
                    attributes_json TEXT NOT NULL,
                    resource_json TEXT NOT NULL,
                    events_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    ingest_sequence INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (trace_id, span_id)
                );

                CREATE INDEX IF NOT EXISTS trace_spans_verification_idx
                ON trace_spans(run_id, scenario_id, variant, start_time_ns);
                CREATE INDEX IF NOT EXISTS trace_spans_session_idx
                ON trace_spans(session_id, start_time_ns);

                CREATE TABLE IF NOT EXISTS log_records (
                    observation_id TEXT PRIMARY KEY,
                    timestamp_ns INTEGER NOT NULL,
                    observed_time_ns INTEGER NOT NULL,
                    trace_id TEXT,
                    span_id TEXT,
                    severity_number INTEGER,
                    severity_text TEXT NOT NULL,
                    body TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    service_version TEXT,
                    session_id TEXT,
                    run_id TEXT,
                    cycle INTEGER,
                    scenario_id TEXT,
                    variant TEXT,
                    input_digest TEXT,
                    collection_id TEXT,
                    event_name TEXT,
                    error_type TEXT,
                    event_code TEXT,
                    message_template TEXT,
                    business_frame TEXT,
                    attributes_json TEXT NOT NULL,
                    resource_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    ingest_sequence INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS log_records_verification_idx
                ON log_records(run_id, scenario_id, variant, timestamp_ns);
                CREATE INDEX IF NOT EXISTS log_records_trace_idx
                ON log_records(trace_id, timestamp_ns);
                CREATE INDEX IF NOT EXISTS log_records_session_idx
                ON log_records(session_id, timestamp_ns);

                CREATE TABLE IF NOT EXISTS execution_windows (
                    run_id TEXT NOT NULL,
                    cycle INTEGER NOT NULL,
                    scenario_id TEXT NOT NULL,
                    variant TEXT NOT NULL CHECK (variant IN ('control', 'candidate')),
                    input_digest TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    collection_id TEXT NOT NULL UNIQUE,
                    control_ref TEXT NOT NULL,
                    control_digest TEXT NOT NULL,
                    candidate_ref TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    skill_digests_json TEXT NOT NULL,
                    started_at_ns INTEGER NOT NULL,
                    ended_at_ns INTEGER NOT NULL,
                    collection_complete INTEGER NOT NULL CHECK (collection_complete IN (0, 1)),
                    trace_id TEXT,
                    request_id TEXT,
                    session_id TEXT,
                    finished INTEGER CHECK (finished IN (0, 1)),
                    outcome TEXT CHECK (outcome IN ('success', 'failure')),
                    payload_json TEXT,
                    model TEXT,
                    tool_calls_json TEXT,
                    oracle_digest TEXT,
                    result_sha256 TEXT,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, cycle, scenario_id, variant)
                );

                CREATE TABLE IF NOT EXISTS import_checkpoints (
                    path TEXT PRIMARY KEY,
                    inode INTEGER NOT NULL,
                    byte_offset INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS otlp_ingest_clock (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    sequence INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO otlp_ingest_clock(singleton, sequence)
                VALUES (1, 0);

                CREATE TABLE IF NOT EXISTS otlp_flush_barriers (
                    flush_id TEXT PRIMARY KEY,
                    collection_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    cycle INTEGER NOT NULL,
                    scenario_id TEXT NOT NULL,
                    variant TEXT NOT NULL CHECK (variant IN ('control', 'candidate')),
                    input_digest TEXT NOT NULL,
                    signals_json TEXT NOT NULL,
                    flush_started_at_ns INTEGER NOT NULL,
                    flush_completed_at_ns INTEGER NOT NULL,
                    deadline_ns INTEGER NOT NULL,
                    received_at_ns INTEGER NOT NULL,
                    watermark_sequence INTEGER NOT NULL,
                    timed_out INTEGER NOT NULL CHECK (timed_out IN (0, 1)),
                    late_arrival_count INTEGER NOT NULL DEFAULT 0,
                    last_late_at_ns INTEGER
                );
                CREATE INDEX IF NOT EXISTS otlp_flush_barriers_collection_idx
                ON otlp_flush_barriers(collection_id);
                """
            )
            rows = connection.execute("SELECT version FROM schema_meta").fetchall()
            if not rows:
                connection.execute(
                    "INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif len(rows) == 1 and rows[0]["version"] in {1, 2, 3, 4}:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(execution_windows)"
                    ).fetchall()
                }
                if "input_json" not in columns:
                    connection.execute(
                        "ALTER TABLE execution_windows ADD COLUMN input_json TEXT"
                    )
                if "oracle_digest" not in columns:
                    connection.execute(
                        "ALTER TABLE execution_windows ADD COLUMN oracle_digest TEXT"
                    )
                if "result_sha256" not in columns:
                    connection.execute(
                        "ALTER TABLE execution_windows ADD COLUMN result_sha256 TEXT"
                    )
                for table in ("trace_spans", "log_records"):
                    table_columns = {
                        row["name"]
                        for row in connection.execute(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                    }
                    if "ingest_sequence" not in table_columns:
                        connection.execute(
                            f"ALTER TABLE {table} "
                            "ADD COLUMN ingest_sequence INTEGER NOT NULL DEFAULT 0"
                        )
                    if "cycle" not in table_columns:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN cycle INTEGER"
                        )
                    if "collection_id" not in table_columns:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN collection_id TEXT"
                        )
                barrier_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(otlp_flush_barriers)"
                    ).fetchall()
                }
                if "late_arrival_count" not in barrier_columns:
                    connection.execute(
                        "ALTER TABLE otlp_flush_barriers "
                        "ADD COLUMN late_arrival_count INTEGER NOT NULL DEFAULT 0"
                    )
                if "last_late_at_ns" not in barrier_columns:
                    connection.execute(
                        "ALTER TABLE otlp_flush_barriers "
                        "ADD COLUMN last_late_at_ns INTEGER"
                    )
                connection.execute(
                    "UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,)
                )
            elif len(rows) != 1 or rows[0]["version"] != SCHEMA_VERSION:
                raise ObservabilityStoreError("不支持的 observability schema 版本")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS trace_spans_collection_idx "
                "ON trace_spans(collection_id, ingest_sequence)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS log_records_collection_idx "
                "ON log_records(collection_id, ingest_sequence)"
            )

    def ingest_otlp_traces(self, payload: Mapping[str, Any]) -> int:
        inserted = 0
        received_at = _now()
        with self._connect() as connection:
            ingest_sequence = self._next_ingest_sequence(connection)
            for resource_group in payload.get("resourceSpans", []):
                resource = _decode_attributes(
                    (resource_group.get("resource") or {}).get("attributes", [])
                )
                for scope_group in resource_group.get("scopeSpans", []):
                    for span in scope_group.get("spans", []):
                        attributes = _decode_attributes(span.get("attributes", []))
                        trace_id = _normalize_identifier(span.get("traceId"), 16)
                        span_id = _normalize_identifier(span.get("spanId"), 8)
                        if not trace_id or not span_id:
                            raise ObservabilityStoreError("OTLP span 缺少 traceId/spanId")
                        if self._reject_if_late(
                            connection, resource=resource, trace_id=trace_id
                        ):
                            continue
                        status = span.get("status") or {}
                        cursor = connection.execute(
                            """
                            INSERT OR IGNORE INTO trace_spans(
                                trace_id, span_id, parent_span_id, name,
                                start_time_ns, end_time_ns, status_code,
                                status_message, service_name, service_version,
                                session_id, run_id, cycle, scenario_id, variant,
                                input_digest, collection_id, attributes_json, resource_json,
                                events_json, raw_json, received_at, ingest_sequence
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                trace_id,
                                span_id,
                                _normalize_identifier(span.get("parentSpanId"), 8),
                                str(span.get("name") or "unknown"),
                                _integer(span.get("startTimeUnixNano")),
                                _integer(span.get("endTimeUnixNano")),
                                str(status.get("code") or ""),
                                str(status.get("message") or ""),
                                str(_lookup(resource, "service.name") or "unknown"),
                                _nullable(_lookup(resource, "service.version")),
                                _nullable(_lookup(attributes, "session.id") or _lookup(resource, "session.id")),
                                _nullable(_lookup(resource, "verification.run_id", "verification_run_id")),
                                _positive_integer_or_none(
                                    _lookup(resource, "verification.cycle", "verification_cycle")
                                ),
                                _nullable(_lookup(resource, "verification.scenario_id", "scenario_id")),
                                _nullable(_lookup(resource, "verification.variant", "variant")),
                                _nullable(_lookup(resource, "verification.input_digest", "input_digest")),
                                _nullable(
                                    _lookup(
                                        resource,
                                        "verification.collection_id",
                                        "verification_collection_id",
                                    )
                                ),
                                _json(attributes),
                                _json(resource),
                                _json(span.get("events", [])),
                                _json(span),
                                received_at,
                                ingest_sequence,
                            ),
                        )
                        inserted += int(cursor.rowcount > 0)
        return inserted

    def ingest_otlp_logs(self, payload: Mapping[str, Any]) -> int:
        inserted = 0
        received_at = _now()
        with self._connect() as connection:
            ingest_sequence = self._next_ingest_sequence(connection)
            for resource_group in payload.get("resourceLogs", []):
                resource = _decode_attributes(
                    (resource_group.get("resource") or {}).get("attributes", [])
                )
                for scope_group in resource_group.get("scopeLogs", []):
                    for record in scope_group.get("logRecords", []):
                        attributes = _decode_attributes(record.get("attributes", []))
                        body_value = _decode_any_value(record.get("body"))
                        body = body_value if isinstance(body_value, str) else _json(body_value)
                        trace_id = _normalize_identifier(record.get("traceId"), 16) or None
                        span_id = _normalize_identifier(record.get("spanId"), 8) or None
                        if self._reject_if_late(
                            connection, resource=resource, trace_id=trace_id
                        ):
                            continue
                        timestamp_ns = _integer(record.get("timeUnixNano"))
                        observed_ns = _integer(record.get("observedTimeUnixNano"), timestamp_ns)
                        event_name = _lookup(attributes, "event.name", "event_name")
                        severity_number = _integer(record.get("severityNumber"))
                        severity_text = _severity_text(
                            record.get("severityText"),
                            severity_number,
                            event_name,
                            attributes,
                        )
                        observation_id = sha256(
                            _json(
                                {
                                    "resource": resource,
                                    "record": record,
                                    "scope": scope_group.get("scope"),
                                }
                            ).encode("utf-8")
                        ).hexdigest()
                        cursor = self._insert_log(
                            connection,
                            observation_id=observation_id,
                            timestamp_ns=timestamp_ns,
                            observed_time_ns=observed_ns,
                            trace_id=trace_id,
                            span_id=span_id,
                            severity_number=severity_number,
                            severity_text=severity_text,
                            body=body,
                            resource=resource,
                            attributes=attributes,
                            raw=record,
                            source="otlp",
                            received_at=received_at,
                            ingest_sequence=ingest_sequence,
                        )
                        inserted += int(cursor.rowcount > 0)
        return inserted

    def _insert_log(
        self,
        connection: sqlite3.Connection,
        *,
        observation_id: str,
        timestamp_ns: int,
        observed_time_ns: int,
        trace_id: str | None,
        span_id: str | None,
        severity_number: int,
        severity_text: str,
        body: str,
        resource: Mapping[str, Any],
        attributes: Mapping[str, Any],
        raw: Any,
        source: str,
        received_at: str,
        ingest_sequence: int = 0,
    ) -> sqlite3.Cursor:
        event_name = _lookup(attributes, "event.name", "event_name")
        diagnostic_message = str(
            _lookup(attributes, "error", "exception.message") or body
        )
        error_type = (
            _error_type(diagnostic_message, attributes)
            if severity_text in {"ERROR", "FATAL"}
            else None
        )
        return connection.execute(
            """
            INSERT OR IGNORE INTO log_records(
                observation_id, timestamp_ns, observed_time_ns, trace_id,
                span_id, severity_number, severity_text, body, service_name,
                service_version, session_id, run_id, cycle, scenario_id, variant,
                input_digest, collection_id, event_name, error_type, event_code,
                message_template, business_frame, attributes_json,
                resource_json, raw_json, source, received_at, ingest_sequence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                timestamp_ns,
                observed_time_ns,
                trace_id,
                span_id,
                severity_number,
                severity_text,
                body,
                str(_lookup(resource, "service.name") or "claude-code"),
                _nullable(_lookup(resource, "service.version")),
                _nullable(_lookup(attributes, "session.id") or _lookup(resource, "session.id")),
                _nullable(_lookup(resource, "verification.run_id", "verification_run_id")),
                _positive_integer_or_none(
                    _lookup(resource, "verification.cycle", "verification_cycle")
                ),
                _nullable(_lookup(resource, "verification.scenario_id", "scenario_id")),
                _nullable(_lookup(resource, "verification.variant", "variant")),
                _nullable(_lookup(resource, "verification.input_digest", "input_digest")),
                _nullable(
                    _lookup(
                        resource,
                        "verification.collection_id",
                        "verification_collection_id",
                    )
                ),
                _nullable(event_name),
                _nullable(error_type),
                _nullable(_lookup(attributes, "event.code", "event_code")),
                _message_template(diagnostic_message),
                _nullable(_lookup(attributes, "code.filepath", "business_frame")),
                _json(attributes),
                _json(resource),
                _json(raw),
                source,
                received_at,
                ingest_sequence,
            ),
        )

    def record_execution(self, window: ExecutionWindow) -> None:
        for name, value in (
            ("run_id", window.run_id),
            ("scenario_id", window.scenario_id),
            ("collection_id", window.collection_id),
            ("control_ref", window.control_ref),
            ("candidate_ref", window.candidate_ref),
        ):
            if not str(value).strip():
                raise ObservabilityStoreError(f"{name} 不能为空")
        if isinstance(window.collection_complete, bool) is False:
            raise ObservabilityStoreError("collection_complete 必须是布尔值")
        if window.finished is not None and isinstance(window.finished, bool) is False:
            raise ObservabilityStoreError("finished 必须是布尔值或 null")
        if window.ended_at_ns < window.started_at_ns:
            raise ObservabilityStoreError("execution window 结束时间早于开始时间")
        if not re.fullmatch(r"[0-9a-f]{64}", window.input_digest):
            raise ObservabilityStoreError("input_digest 必须是 SHA-256")
        if normalized_input_digest(window.input_payload) != window.input_digest:
            raise ObservabilityStoreError("input_payload 与 input_digest 不一致")
        for name, digest in (
            ("control_digest", window.control_digest),
            ("candidate_digest", window.candidate_digest),
            ("policy_digest", window.policy_digest),
            ("oracle_digest", window.oracle_digest),
            ("result_sha256", window.result_sha256),
        ):
            if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ObservabilityStoreError(f"{name} 必须是 SHA-256")
        if not window.skill_digests or any(
            not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in window.skill_digests.values()
        ):
            raise ObservabilityStoreError("skill_digests 非法")
        trace_id = None
        if window.trace_id:
            trace_id = _normalize_identifier(window.trace_id, 16)
            if not trace_id:
                raise ObservabilityStoreError("trace_id 必须是 16 字节 hex/base64")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_windows(
                    run_id, cycle, scenario_id, variant, input_digest, input_json,
                    collection_id, control_ref, control_digest, candidate_ref,
                    candidate_digest, policy_digest, skill_digests_json,
                    started_at_ns, ended_at_ns, collection_complete,
                    trace_id, request_id, session_id, finished, outcome,
                    payload_json, model, tool_calls_json, oracle_digest,
                    result_sha256, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    window.run_id,
                    window.cycle,
                    window.scenario_id,
                    window.variant,
                    window.input_digest,
                    _json(window.input_payload),
                    window.collection_id,
                    window.control_ref,
                    window.control_digest,
                    window.candidate_ref,
                    window.candidate_digest,
                    window.policy_digest,
                    _json(dict(window.skill_digests)),
                    window.started_at_ns,
                    window.ended_at_ns,
                    int(window.collection_complete),
                    trace_id,
                    window.request_id,
                    window.session_id,
                    None if window.finished is None else int(window.finished),
                    window.outcome,
                    None if window.payload is None else _json(window.payload),
                    window.model,
                    None if window.tool_calls is None else _json(window.tool_calls),
                    window.oracle_digest,
                    window.result_sha256,
                    _now(),
                ),
            )

    def execution_windows(self, run_id: str, cycle: int) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT * FROM execution_windows
                WHERE run_id = ? AND cycle = ?
                ORDER BY scenario_id, variant
                """,
                (run_id, cycle),
            ).fetchall()

    def record_otlp_flush_barrier(self, barrier: OtlpFlushBarrier) -> None:
        """Capture a store-side watermark after a trusted caller force-flushes OTLP."""

        for name in (
            "cycle",
            "flush_started_at_ns",
            "flush_completed_at_ns",
            "deadline_ns",
        ):
            value = getattr(barrier, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ObservabilityStoreError(f"{name} 必须是非负整数")
        if barrier.cycle < 1:
            raise ObservabilityStoreError("cycle 必须大于零")
        for name in ("flush_id", "collection_id", "run_id", "scenario_id"):
            if not str(getattr(barrier, name)).strip():
                raise ObservabilityStoreError(f"{name} 不能为空")
        if barrier.variant not in {"control", "candidate"}:
            raise ObservabilityStoreError("variant 必须是 control 或 candidate")
        if not re.fullmatch(r"[0-9a-f]{64}", barrier.input_digest):
            raise ObservabilityStoreError("input_digest 必须是 SHA-256")
        if (
            not barrier.signals
            or len(barrier.signals) != len(set(barrier.signals))
            or not set(barrier.signals).issubset({"traces", "logs"})
        ):
            raise ObservabilityStoreError("signals 必须是无重复的 traces/logs")
        if barrier.flush_completed_at_ns < barrier.flush_started_at_ns:
            raise ObservabilityStoreError("flush 完成时间早于开始时间")

        received_at_ns = time.time_ns()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sequence FROM otlp_ingest_clock WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise ObservabilityStoreError("OTLP ingest clock 未初始化")
            timed_out = (
                barrier.flush_completed_at_ns > barrier.deadline_ns
                or received_at_ns > barrier.deadline_ns
            )
            connection.execute(
                """
                INSERT INTO otlp_flush_barriers(
                    flush_id, collection_id, run_id, cycle, scenario_id,
                    variant, input_digest, signals_json, flush_started_at_ns,
                    flush_completed_at_ns, deadline_ns, received_at_ns,
                    watermark_sequence, timed_out
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    barrier.flush_id,
                    barrier.collection_id,
                    barrier.run_id,
                    barrier.cycle,
                    barrier.scenario_id,
                    barrier.variant,
                    barrier.input_digest,
                    _json(sorted(barrier.signals)),
                    barrier.flush_started_at_ns,
                    barrier.flush_completed_at_ns,
                    barrier.deadline_ns,
                    received_at_ns,
                    int(row["sequence"]),
                    int(timed_out),
                ),
            )

    def wait_for_otlp_flush_barrier(
        self,
        collection_id: str,
        *,
        timeout_ms: int,
        poll_interval_ms: int = 10,
    ) -> sqlite3.Row:
        """Wait for one barrier; absence, timeout, or duplicates are hard errors."""

        if isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 900_000:
            raise ObservabilityStoreError("timeout_ms 必须在 1..900000")
        if isinstance(poll_interval_ms, bool) or not 1 <= poll_interval_ms <= 1000:
            raise ObservabilityStoreError("poll_interval_ms 必须在 1..1000")
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM otlp_flush_barriers WHERE collection_id = ?",
                    (collection_id,),
                ).fetchall()
            if len(rows) > 1:
                raise ObservabilityStoreError("OTLP flush barrier 不唯一")
            if len(rows) == 1:
                if rows[0]["timed_out"]:
                    raise ObservabilityStoreError("OTLP flush barrier 已超时")
                return rows[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ObservabilityStoreError("等待 OTLP flush barrier 超时")
            time.sleep(min(poll_interval_ms / 1000, remaining))

    def otlp_barrier_error(self, window: Mapping[str, Any]) -> str | None:
        """Validate uniqueness, binding, watermark coverage, and late arrivals."""

        with self._connect() as connection:
            barriers = connection.execute(
                "SELECT * FROM otlp_flush_barriers WHERE collection_id = ?",
                (window["collection_id"],),
            ).fetchall()
            if not barriers:
                return "缺少 OTLP flush/watermark barrier"
            if len(barriers) != 1:
                return "OTLP flush/watermark barrier 不唯一"
            barrier = barriers[0]
            expected = {
                "collection_id": window["collection_id"],
                "run_id": window["run_id"],
                "cycle": window["cycle"],
                "scenario_id": window["scenario_id"],
                "variant": window["variant"],
                "input_digest": window["input_digest"],
            }
            mismatches = [
                name for name, value in expected.items() if barrier[name] != value
            ]
            if mismatches:
                return "OTLP barrier 绑定不一致: " + ", ".join(mismatches)
            if barrier["timed_out"]:
                return "OTLP flush/watermark barrier 超时"
            if int(barrier["late_arrival_count"]):
                return "OTLP watermark 后检测到晚到数据"
            try:
                signals = json.loads(barrier["signals_json"])
            except (TypeError, json.JSONDecodeError):
                return "OTLP barrier signals 非法"
            if signals != ["logs", "traces"]:
                return "OTLP barrier 未同时确认 traces/logs flush"
            if (
                barrier["flush_started_at_ns"] < window["started_at_ns"]
                or barrier["flush_completed_at_ns"] < barrier["flush_started_at_ns"]
                or barrier["flush_completed_at_ns"] > barrier["deadline_ns"]
                or barrier["received_at_ns"] > barrier["deadline_ns"]
            ):
                return "OTLP barrier 时间窗口非法或超时"

            watermark = int(barrier["watermark_sequence"])
            clauses = (
                "collection_id = ? AND run_id = ? AND cycle = ? "
                "AND scenario_id = ? AND variant = ? AND input_digest = ?"
            )
            params = (
                window["collection_id"],
                window["run_id"],
                window["cycle"],
                window["scenario_id"],
                window["variant"],
                window["input_digest"],
            )
            trace_stats = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN ingest_sequence > ? THEN 1 ELSE 0 END) AS late "
                "FROM trace_spans WHERE " + clauses,
                (watermark, *params),
            ).fetchone()
            log_stats = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN ingest_sequence > ? THEN 1 ELSE 0 END) AS late "
                "FROM log_records WHERE source = 'otlp' AND " + clauses,
                (watermark, *params),
            ).fetchone()
            if not trace_stats["total"] or not log_stats["total"]:
                return "OTLP barrier 前 traces/logs 覆盖不完整"
            if int(trace_stats["late"] or 0) or int(log_stats["late"] or 0):
                return "OTLP watermark 后检测到晚到数据"
        return None

    def otlp_barrier_digest(self, window: Mapping[str, Any]) -> str:
        """Return the digest of the currently valid, immutable barrier snapshot."""

        error = self.otlp_barrier_error(window)
        if error:
            raise ObservabilityStoreError(error)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            barriers = connection.execute(
                "SELECT * FROM otlp_flush_barriers WHERE collection_id = ?",
                (window["collection_id"],),
            ).fetchall()
            if len(barriers) != 1:
                raise ObservabilityStoreError("OTLP flush/watermark barrier 不唯一")
            barrier = barriers[0]
            if barrier["timed_out"] or int(barrier["late_arrival_count"]):
                raise ObservabilityStoreError("OTLP barrier 已失效")
            payload = {
                key: barrier[key]
                for key in (
                    "flush_id",
                    "collection_id",
                    "run_id",
                    "cycle",
                    "scenario_id",
                    "variant",
                    "input_digest",
                    "signals_json",
                    "flush_started_at_ns",
                    "flush_completed_at_ns",
                    "deadline_ns",
                    "received_at_ns",
                    "watermark_sequence",
                    "timed_out",
                    "late_arrival_count",
                )
            }
        return sha256(_json(payload).encode("utf-8")).hexdigest()

    def trace_rows_for_window(self, window: Mapping[str, Any]) -> list[sqlite3.Row]:
        clauses = [
            "start_time_ns <= ?",
            "end_time_ns >= ?",
            "collection_id = ?",
        ]
        params: list[Any] = [
            window["ended_at_ns"],
            window["started_at_ns"],
            window["collection_id"],
        ]
        if window.get("trace_id"):
            clauses.append("trace_id = ?")
            params.append(window["trace_id"])
        else:
            clauses.extend(
                ["run_id = ?", "scenario_id = ?", "variant = ?"]
            )
            params.extend(
                [window["run_id"], window["scenario_id"], window["variant"]]
            )
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM trace_spans WHERE " + " AND ".join(clauses) +
                " ORDER BY start_time_ns, span_id",
                params,
            ).fetchall()

    def log_rows_for_window(self, window: Mapping[str, Any]) -> list[sqlite3.Row]:
        correlation: list[str] = []
        params: list[Any] = [
            window["started_at_ns"],
            window["ended_at_ns"],
            window["collection_id"],
        ]
        if window.get("trace_id"):
            correlation.append("trace_id = ?")
            params.append(window["trace_id"])
        if window.get("session_id"):
            correlation.append("session_id = ?")
            params.append(window["session_id"])
        correlation.append("(run_id = ? AND scenario_id = ? AND variant = ?)")
        params.extend([window["run_id"], window["scenario_id"], window["variant"]])
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT * FROM log_records
                WHERE timestamp_ns BETWEEN ? AND ?
                  AND collection_id = ?
                  AND (""" + " OR ".join(correlation) + ")"
                " ORDER BY timestamp_ns, observation_id",
                params,
            ).fetchall()

    def trace_payload(self, trace_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM trace_spans WHERE trace_id = ? ORDER BY start_time_ns",
                (trace_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_logs(
        self,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        scenario_id: str | None = None,
        variant: str | None = None,
        service_name: str | None = None,
        level: str | None = None,
        start_time_ns: int | None = None,
        end_time_ns: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ObservabilityStoreError("limit 必须在 1..1000")
        if variant is not None and variant not in {"control", "candidate"}:
            raise ObservabilityStoreError("variant 必须是 control 或 candidate")
        for name, value in (
            ("start_time_ns", start_time_ns),
            ("end_time_ns", end_time_ns),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ObservabilityStoreError(f"{name} 必须是非负整数")
        if (
            start_time_ns is not None
            and end_time_ns is not None
            and end_time_ns < start_time_ns
        ):
            raise ObservabilityStoreError("end_time_ns 不能早于 start_time_ns")
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("trace_id", trace_id),
            ("session_id", session_id),
            ("run_id", run_id),
            ("scenario_id", scenario_id),
            ("variant", variant),
            ("service_name", service_name),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if level:
            clauses.append("severity_text = ?")
            params.append(level.upper())
        if start_time_ns is not None:
            clauses.append("timestamp_ns >= ?")
            params.append(start_time_ns)
        if end_time_ns is not None:
            clauses.append("timestamp_ns <= ?")
            params.append(end_time_ns)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM log_records" + where +
                " ORDER BY timestamp_ns DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]


def _nullable(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_integer_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_false(value: Any) -> bool:
    return value is False or (
        isinstance(value, str) and value.strip().lower() == "false"
    )


def _severity_text(
    value: Any,
    number: int,
    event_name: Any,
    attributes: Mapping[str, Any] | None = None,
) -> str:
    explicit = str(value or "").strip().upper()
    if explicit in {"ERROR", "FATAL"}:
        return explicit
    if number >= 21:
        return "FATAL"
    if number >= 17:
        return "ERROR"
    attributes = attributes or {}
    if _is_false(_lookup(attributes, "success", "ok")):
        return "ERROR"
    if _lookup(attributes, "error", "error.type", "exception.type"):
        return "ERROR"
    if explicit:
        return "WARN" if explicit == "WARNING" else explicit
    if number >= 13:
        return "WARN"
    if number >= 9:
        return "INFO"
    if number >= 5:
        return "DEBUG"
    event = str(event_name or "").lower()
    if re.search(r"(?:^|[._-])(?:error|failed|failure)(?:$|[._-])", event):
        return "ERROR"
    return "INFO"


class CCBDebugLogImporter:
    """Incrementally imports CCB `--debug` text files into the local store."""

    def __init__(self, store: LocalObservabilityStore):
        self.store = store

    def import_directory(self, directory: str | Path) -> int:
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise ObservabilityStoreError(f"CCB debug 日志目录不存在: {root}")
        return sum(
            self.import_file(path)
            for path in sorted(root.glob("*.txt"))
            if path.name != "latest" and not path.is_symlink()
        )

    def import_file(self, path: str | Path) -> int:
        source = Path(path).expanduser().resolve()
        stat = source.stat()
        with self.store._connect() as connection:
            checkpoint = connection.execute(
                "SELECT inode, byte_offset FROM import_checkpoints WHERE path = ?",
                (str(source),),
            ).fetchone()
            offset = 0
            if checkpoint and checkpoint["inode"] == stat.st_ino:
                previous = checkpoint["byte_offset"]
                offset = previous if stat.st_size >= previous else 0
            session_id = source.stem
            inserted = 0
            with source.open("rb") as handle:
                handle.seek(offset)
                new_offset = offset
                for raw_line in handle:
                    line_offset = handle.tell() - len(raw_line)
                    if not raw_line.endswith(b"\n"):
                        break
                    new_offset = handle.tell()
                    try:
                        line = raw_line.decode("utf-8").rstrip("\r\n")
                    except UnicodeDecodeError:
                        continue
                    match = _DEBUG_LINE.match(line)
                    if not match:
                        continue
                    timestamp_ns = _iso_to_ns(match.group("timestamp"))
                    level = match.group("level")
                    message = match.group("message")
                    observation_id = sha256(
                        f"{source}:{stat.st_ino}:{line_offset}:{line}".encode("utf-8")
                    ).hexdigest()
                    cursor = self.store._insert_log(
                        connection,
                        observation_id=observation_id,
                        timestamp_ns=timestamp_ns,
                        observed_time_ns=timestamp_ns,
                        trace_id=None,
                        span_id=None,
                        severity_number=0,
                        severity_text="WARN" if level == "WARN" else level,
                        body=message,
                        resource={"service.name": "claude-code"},
                        attributes={"session.id": session_id},
                        raw={"line": line, "path": str(source)},
                        source="ccb-debug",
                        received_at=_now(),
                    )
                    inserted += int(cursor.rowcount > 0)
            connection.execute(
                """
                INSERT INTO import_checkpoints(path, inode, byte_offset, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    inode = excluded.inode,
                    byte_offset = excluded.byte_offset,
                    updated_at = excluded.updated_at
                """,
                (str(source), stat.st_ino, new_offset, _now()),
            )
        return inserted


def _iso_to_ns(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


__all__ = [
    "CCBDebugLogImporter",
    "ExecutionWindow",
    "LocalObservabilityStore",
    "ObservabilityStoreError",
    "OtlpFlushBarrier",
    "normalized_input_digest",
]
