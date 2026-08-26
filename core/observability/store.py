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
from typing import Any, Literal


SCHEMA_VERSION = 2
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
                    scenario_id TEXT,
                    variant TEXT,
                    input_digest TEXT,
                    attributes_json TEXT NOT NULL,
                    resource_json TEXT NOT NULL,
                    events_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
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
                    scenario_id TEXT,
                    variant TEXT,
                    input_digest TEXT,
                    event_name TEXT,
                    error_type TEXT,
                    event_code TEXT,
                    message_template TEXT,
                    business_frame TEXT,
                    attributes_json TEXT NOT NULL,
                    resource_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    received_at TEXT NOT NULL
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
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, cycle, scenario_id, variant)
                );

                CREATE TABLE IF NOT EXISTS import_checkpoints (
                    path TEXT PRIMARY KEY,
                    inode INTEGER NOT NULL,
                    byte_offset INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            rows = connection.execute("SELECT version FROM schema_meta").fetchall()
            if not rows:
                connection.execute(
                    "INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif len(rows) == 1 and rows[0]["version"] == 1:
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
                connection.execute(
                    "UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,)
                )
            elif len(rows) != 1 or rows[0]["version"] != SCHEMA_VERSION:
                raise ObservabilityStoreError("不支持的 observability schema 版本")

    def ingest_otlp_traces(self, payload: Mapping[str, Any]) -> int:
        inserted = 0
        received_at = _now()
        with self._connect() as connection:
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
                        status = span.get("status") or {}
                        cursor = connection.execute(
                            """
                            INSERT OR IGNORE INTO trace_spans(
                                trace_id, span_id, parent_span_id, name,
                                start_time_ns, end_time_ns, status_code,
                                status_message, service_name, service_version,
                                session_id, run_id, scenario_id, variant,
                                input_digest, attributes_json, resource_json,
                                events_json, raw_json, received_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                _nullable(_lookup(resource, "verification.scenario_id", "scenario_id")),
                                _nullable(_lookup(resource, "verification.variant", "variant")),
                                _nullable(_lookup(resource, "verification.input_digest", "input_digest")),
                                _json(attributes),
                                _json(resource),
                                _json(span.get("events", [])),
                                _json(span),
                                received_at,
                            ),
                        )
                        inserted += int(cursor.rowcount > 0)
        return inserted

    def ingest_otlp_logs(self, payload: Mapping[str, Any]) -> int:
        inserted = 0
        received_at = _now()
        with self._connect() as connection:
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
                service_version, session_id, run_id, scenario_id, variant,
                input_digest, event_name, error_type, event_code,
                message_template, business_frame, attributes_json,
                resource_json, raw_json, source, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                _nullable(_lookup(resource, "verification.scenario_id", "scenario_id")),
                _nullable(_lookup(resource, "verification.variant", "variant")),
                _nullable(_lookup(resource, "verification.input_digest", "input_digest")),
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
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
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
                    payload_json, model, tool_calls_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def trace_rows_for_window(self, window: Mapping[str, Any]) -> list[sqlite3.Row]:
        clauses = [
            "start_time_ns <= ?",
            "end_time_ns >= ?",
        ]
        params: list[Any] = [window["ended_at_ns"], window["started_at_ns"]]
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
        params: list[Any] = [window["started_at_ns"], window["ended_at_ns"]]
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
                for raw_line in handle:
                    line_offset = handle.tell() - len(raw_line)
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
                new_offset = handle.tell()
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
    "normalized_input_digest",
]
