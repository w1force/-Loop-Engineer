"""Durable orchestration state — SQLite WAL store for the Loop Engineer control plane.

This is the "持久化" layer of PRD §16, distinct from the verification *evidence*
store (`core.observability`, which holds trace/log spans for gates). It holds the
discovery + orchestration state that must live outside any agent conversation:

    source_cursors  connector incremental read positions (inode/byte offset)
    signals         raw detected signals, deduped by signal_id
    incidents       deduped by fingerprint, with a suppression window (FR-AUTO-004)
    loop_runs       one run per incident attempt; optimistic-locked state machine
    run_events      append-only per-run event log
    artifacts       content-addressed JSON payloads (the immutable artifact chain)
    outbox          external side-effect intents, idempotent by idempotency_key

Every public operation runs in a single transaction. Mirrors the WAL/pragma setup
used by `core.observability.store.LocalObservabilityStore`.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


class LoopStateError(RuntimeError):
    """State-store invariant violated (integrity block, CAS conflict, digest drift)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def content_digest(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IncidentRecord:
    incident_id: str
    fingerprint: str
    matched_rule: str
    severity: str
    eligibility: str
    service: str
    status: str
    version: int
    occurrences: int
    first_seen: str
    last_seen: str
    sample: dict[str, Any]
    created: bool  # True if this call created it (vs. deduped into an existing one)
    signals: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    incident_id: str
    state: str
    version: int
    attempt: int
    request_digest: str
    created_at: str


@dataclass(frozen=True)
class SignalIngestResult:
    incident: IncidentRecord
    signal_created: bool
    role: str
    correlation_reason: str


class LoopStateStore:
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

    @contextmanager
    def _tx(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
            else:
                with connection:  # commit on success, rollback on exception
                    yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_cursors (
                    source_id   TEXT PRIMARY KEY,
                    path        TEXT NOT NULL,
                    inode       INTEGER NOT NULL,
                    byte_offset INTEGER NOT NULL,
                    updated_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signals (
                    signal_id   TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    source_id   TEXT NOT NULL,
                    kind        TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload     TEXT NOT NULL,
                    incident_id TEXT
                );
                CREATE INDEX IF NOT EXISTS signals_fingerprint
                    ON signals(fingerprint);

                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    matched_rule TEXT NOT NULL,
                    severity    TEXT NOT NULL,
                    eligibility TEXT NOT NULL,
                    service     TEXT NOT NULL,
                    status      TEXT NOT NULL,
                    version     INTEGER NOT NULL,
                    occurrences INTEGER NOT NULL,
                    first_seen  TEXT NOT NULL,
                    last_seen   TEXT NOT NULL,
                    sample      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS incidents_open_fingerprint
                    ON incidents(fingerprint, status, last_seen);

                CREATE TABLE IF NOT EXISTS incident_signals (
                    signal_id          TEXT PRIMARY KEY,
                    incident_id        TEXT NOT NULL,
                    role               TEXT NOT NULL CHECK (role IN ('primary', 'secondary')),
                    fingerprint        TEXT NOT NULL,
                    observed_at        TEXT NOT NULL,
                    trace_id           TEXT,
                    request_id         TEXT,
                    run_id             TEXT,
                    session_id         TEXT,
                    erp                TEXT,
                    environment        TEXT,
                    deployment_version TEXT,
                    correlation_reason TEXT NOT NULL,
                    processing_state   TEXT NOT NULL DEFAULT 'attached',
                    attached_at        TEXT NOT NULL,
                    FOREIGN KEY(signal_id) REFERENCES signals(signal_id),
                    FOREIGN KEY(incident_id) REFERENCES incidents(incident_id)
                );
                CREATE INDEX IF NOT EXISTS incident_signals_incident_idx
                    ON incident_signals(incident_id, role, observed_at);
                CREATE INDEX IF NOT EXISTS incident_signals_trace_idx
                    ON incident_signals(trace_id, observed_at);
                CREATE INDEX IF NOT EXISTS incident_signals_request_idx
                    ON incident_signals(request_id, observed_at);
                CREATE INDEX IF NOT EXISTS incident_signals_run_idx
                    ON incident_signals(run_id, observed_at);
                CREATE INDEX IF NOT EXISTS incident_signals_session_idx
                    ON incident_signals(session_id, observed_at);
                CREATE INDEX IF NOT EXISTS incident_signals_erp_idx
                    ON incident_signals(erp, observed_at);
                CREATE UNIQUE INDEX IF NOT EXISTS incident_signals_one_primary
                    ON incident_signals(incident_id) WHERE role = 'primary';

                CREATE TABLE IF NOT EXISTS diagnosis_attempts (
                    incident_id       TEXT NOT NULL,
                    attempt           INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 3),
                    hypothesis_digest TEXT NOT NULL,
                    disposition       TEXT NOT NULL,
                    summary           TEXT NOT NULL,
                    evidence_refs     TEXT NOT NULL,
                    created_at        TEXT NOT NULL,
                    PRIMARY KEY(incident_id, attempt),
                    UNIQUE(incident_id, hypothesis_digest),
                    FOREIGN KEY(incident_id) REFERENCES incidents(incident_id)
                );

                CREATE TABLE IF NOT EXISTS incident_resolutions (
                    incident_id          TEXT PRIMARY KEY,
                    verified_candidate   TEXT NOT NULL,
                    deployed_version     TEXT,
                    verified_at          TEXT NOT NULL,
                    deployed_at          TEXT,
                    FOREIGN KEY(incident_id) REFERENCES incidents(incident_id)
                );

                CREATE TABLE IF NOT EXISTS loop_runs (
                    run_id         TEXT PRIMARY KEY,
                    incident_id    TEXT NOT NULL,
                    state          TEXT NOT NULL,
                    version        INTEGER NOT NULL,
                    attempt        INTEGER NOT NULL,
                    request_digest TEXT NOT NULL,
                    created_at     TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    run_id     TEXT NOT NULL,
                    seq        INTEGER NOT NULL,
                    kind       TEXT NOT NULL,
                    payload    TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    digest        TEXT PRIMARY KEY,
                    artifact_type TEXT NOT NULL,
                    run_id        TEXT,
                    incident_id   TEXT,
                    payload       TEXT NOT NULL,
                    created_at    TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    idempotency_key TEXT PRIMARY KEY,
                    action_type     TEXT NOT NULL,
                    request_digest  TEXT NOT NULL,
                    payload         TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    external_id     TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
                """
            )
            self._backfill_incident_signals(connection)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _backfill_incident_signals(connection: sqlite3.Connection) -> None:
        """Upgrade legacy signal rows without rewriting their immutable payload."""

        relation_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(incident_signals)"
            ).fetchall()
        }
        for column in ("environment", "deployment_version"):
            if column not in relation_columns:
                connection.execute(
                    f"ALTER TABLE incident_signals ADD COLUMN {column} TEXT"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS incident_signals_scope_idx "
            "ON incident_signals(environment, deployment_version, observed_at)"
        )

        rows = connection.execute(
            """
            SELECT signal.*, incident.status AS incident_status
            FROM signals AS signal
            JOIN incidents AS incident ON incident.incident_id = signal.incident_id
            LEFT JOIN incident_signals AS relation
              ON relation.signal_id = signal.signal_id
            WHERE relation.signal_id IS NULL
            ORDER BY signal.incident_id, signal.observed_at, signal.signal_id
            """
        ).fetchall()
        primary_by_incident = {
            row["incident_id"]
            for row in connection.execute(
                "SELECT incident_id FROM incident_signals WHERE role = 'primary'"
            ).fetchall()
        }
        attached_at = _now()
        for row in rows:
            incident_id = row["incident_id"]
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            role = "secondary" if incident_id in primary_by_incident else "primary"
            primary_by_incident.add(incident_id)
            connection.execute(
                """
                INSERT INTO incident_signals(
                    signal_id, incident_id, role, fingerprint, observed_at,
                    trace_id, request_id, run_id, session_id, erp,
                    environment, deployment_version, correlation_reason, processing_state,
                    attached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'migration', ?, ?)
                """,
                (
                    row["signal_id"],
                    incident_id,
                    role,
                    row["fingerprint"],
                    row["observed_at"],
                    _clean_identifier(payload.get("trace_id")),
                    _clean_identifier(payload.get("request_id")),
                    _clean_identifier(payload.get("run_id")),
                    _clean_identifier(payload.get("session_id")),
                    _clean_identifier(payload.get("erp")),
                    _clean_identifier(payload.get("environment")),
                    _clean_identifier(payload.get("deployment_version")),
                    "processed" if row["incident_status"] == "resolved" else "attached",
                    attached_at,
                ),
            )

        # A database created by an earlier incident-correlation build can already
        # contain relation rows while lacking the new scope columns.  Backfill
        # those rows from the immutable signal payload as well; otherwise a restart
        # would silently place old rows in the NULL scope and break dedup isolation.
        scoped_rows = connection.execute(
            """
            SELECT relation.signal_id, relation.environment,
                   relation.deployment_version, signal.payload
            FROM incident_signals AS relation
            JOIN signals AS signal ON signal.signal_id = relation.signal_id
            WHERE relation.environment IS NULL
               OR relation.deployment_version IS NULL
            """
        ).fetchall()
        scoped_updates: list[tuple[str | None, str | None, str]] = []
        for row in scoped_rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            scoped_updates.append(
                (
                    row["environment"]
                    or _clean_identifier(payload.get("environment")),
                    row["deployment_version"]
                    or _clean_identifier(payload.get("deployment_version")),
                    row["signal_id"],
                )
            )
        connection.executemany(
            """
            UPDATE incident_signals
            SET environment = ?, deployment_version = ?
            WHERE signal_id = ?
            """,
            scoped_updates,
        )

    # ── connector cursors ─────────────────────────────────────────────────────
    def get_cursor(self, source_id: str) -> dict[str, Any] | None:
        with self._tx() as connection:
            row = connection.execute(
                "SELECT path, inode, byte_offset, updated_at FROM source_cursors"
                " WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_cursor(
        self, source_id: str, *, path: str, inode: int, byte_offset: int
    ) -> None:
        with self._tx() as connection:
            connection.execute(
                """
                INSERT INTO source_cursors(source_id, path, inode, byte_offset, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    path = excluded.path,
                    inode = excluded.inode,
                    byte_offset = excluded.byte_offset,
                    updated_at = excluded.updated_at
                """,
                (source_id, path, int(inode), int(byte_offset), _now()),
            )

    # ── signals ────────────────────────────────────────────────────────────────
    def record_signal(
        self,
        *,
        signal_id: str,
        fingerprint: str,
        source_id: str,
        kind: str,
        observed_at: str,
        payload: dict[str, Any],
        incident_id: str | None = None,
    ) -> bool:
        """Insert a raw signal; idempotent by signal_id. Returns True if newly inserted."""

        with self._tx() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO signals(
                    signal_id, fingerprint, source_id, kind, observed_at, payload, incident_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    fingerprint,
                    source_id,
                    kind,
                    observed_at,
                    _canonical(payload),
                    incident_id,
                ),
            )
            return cursor.rowcount > 0

    def ingest_signal(
        self,
        *,
        signal_id: str,
        fingerprint: str,
        source_id: str,
        kind: str,
        observed_at: str,
        payload: dict[str, Any],
        matched_rule: str,
        severity: str,
        eligibility: str,
        service: str,
        trace_id: str | None = None,
        request_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        erp: str | None = None,
        environment: str | None = None,
        deployment_version: str | None = None,
        suppression_seconds: int = 3600,
        open_states: tuple[str, ...] = ("open",),
    ) -> SignalIngestResult:
        """Atomically dedupe a signal and attach it to one causal incident.

        Exact signal idempotency is checked *before* incrementing an incident.  This
        avoids full-rescan inflation.  Strong correlation identifiers may attach a
        different fingerprint to the same incident; fingerprint remains the final
        fallback when no causal identifier is shared.
        """

        if suppression_seconds < 0:
            raise LoopStateError("suppression_seconds must be non-negative")
        if not open_states:
            raise LoopStateError("open_states cannot be empty")
        now = _now()
        event_time = _normalize_timestamp(observed_at) or now
        identity = {
            "trace_id": _clean_identifier(trace_id),
            "request_id": _clean_identifier(request_id),
            "run_id": _clean_identifier(run_id),
            "session_id": _clean_identifier(session_id),
            "erp": _clean_identifier(erp),
        }
        normalized_environment = _clean_identifier(environment)
        normalized_version = _clean_identifier(deployment_version)
        with self._tx(immediate=True) as connection:
            existing_signal = connection.execute(
                "SELECT incident_id FROM signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
            if existing_signal is not None:
                incident_row = connection.execute(
                    "SELECT * FROM incidents WHERE incident_id = ?",
                    (existing_signal["incident_id"],),
                ).fetchone()
                if incident_row is None:
                    raise LoopStateError(
                        f"signal {signal_id} references a missing incident"
                    )
                relation = connection.execute(
                    "SELECT role, correlation_reason FROM incident_signals "
                    "WHERE signal_id = ?",
                    (signal_id,),
                ).fetchone()
                return SignalIngestResult(
                    incident=_incident_from_row(
                        incident_row,
                        created=False,
                        signals=self._list_incident_signals(
                            connection, incident_row["incident_id"]
                        ),
                    ),
                    signal_created=False,
                    role=relation["role"] if relation else "secondary",
                    correlation_reason=(
                        relation["correlation_reason"] if relation else "signal_id"
                    ),
                )

            incident_row, correlation_reason = self._find_correlated_incident(
                connection,
                fingerprint=fingerprint,
                service=service,
                identity=identity,
                environment=normalized_environment,
                deployment_version=normalized_version,
                event_time=event_time,
                suppression_seconds=suppression_seconds,
                open_states=open_states,
            )
            if incident_row is None:
                incident_id = "incident-" + content_digest(
                    {
                        "fp": fingerprint,
                        "first_seen": event_time,
                        "service": service,
                        "signal_id": signal_id,
                    }
                )[:20]
                connection.execute(
                    """
                    INSERT INTO incidents(
                        incident_id, fingerprint, matched_rule, severity, eligibility,
                        service, status, version, occurrences, first_seen, last_seen, sample
                    ) VALUES (?, ?, ?, ?, ?, ?, 'open', 1, 1, ?, ?, ?)
                    """,
                    (
                        incident_id,
                        fingerprint,
                        matched_rule,
                        severity,
                        eligibility,
                        service,
                        event_time,
                        event_time,
                        _canonical(payload),
                    ),
                )
                role = "primary"
                correlation_reason = "new_incident"
                created = True
            else:
                incident_id = incident_row["incident_id"]
                first_seen = min(incident_row["first_seen"], event_time)
                last_seen = max(incident_row["last_seen"], event_time)
                effective_severity = _higher_severity(
                    str(incident_row["severity"]), severity
                )
                effective_eligibility = _more_restrictive_eligibility(
                    str(incident_row["eligibility"]), eligibility
                )
                connection.execute(
                    """
                    UPDATE incidents
                    SET occurrences = occurrences + 1, first_seen = ?, last_seen = ?,
                        severity = ?, eligibility = ?, version = version + 1
                    WHERE incident_id = ?
                    """,
                    (
                        first_seen,
                        last_seen,
                        effective_severity,
                        effective_eligibility,
                        incident_id,
                    ),
                )
                role = "secondary"
                created = False

            connection.execute(
                """
                INSERT INTO signals(
                    signal_id, fingerprint, source_id, kind, observed_at, payload, incident_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    fingerprint,
                    source_id,
                    kind,
                    event_time,
                    _canonical(payload),
                    incident_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO incident_signals(
                    signal_id, incident_id, role, fingerprint, observed_at,
                    trace_id, request_id, run_id, session_id, erp,
                    environment, deployment_version, correlation_reason,
                    processing_state, attached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'attached', ?)
                """,
                (
                    signal_id,
                    incident_id,
                    role,
                    fingerprint,
                    event_time,
                    identity["trace_id"],
                    identity["request_id"],
                    identity["run_id"],
                    identity["session_id"],
                    identity["erp"],
                    normalized_environment,
                    normalized_version,
                    correlation_reason,
                    now,
                ),
            )
            current = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            return SignalIngestResult(
                incident=_incident_from_row(
                    current,
                    created=created,
                    signals=self._list_incident_signals(connection, incident_id),
                ),
                signal_created=True,
                role=role,
                correlation_reason=correlation_reason,
            )

    @staticmethod
    def _find_correlated_incident(
        connection: sqlite3.Connection,
        *,
        fingerprint: str,
        service: str,
        identity: dict[str, str | None],
        environment: str | None,
        deployment_version: str | None,
        event_time: str,
        suppression_seconds: int,
        open_states: tuple[str, ...],
    ) -> tuple[sqlite3.Row | None, str]:
        placeholders = ",".join("?" for _ in open_states)
        # Scope is part of incident identity.  Missing metadata is an explicit
        # unknown scope, not permission to attach to any environment/version.
        # SQLite's ``IS ?`` gives the desired NULL-safe equality semantics.
        scope_sql = (
            " AND signal.environment IS ?"
            " AND signal.deployment_version IS ?"
        )
        scope_params: tuple[str | None, str | None] = (
            environment,
            deployment_version,
        )
        for field in ("trace_id", "request_id", "run_id", "session_id"):
            value = identity[field]
            if not value:
                continue
            rows = connection.execute(
                f"""
                SELECT DISTINCT incident.*
                FROM incident_signals AS signal
                JOIN incidents AS incident
                  ON incident.incident_id = signal.incident_id
                WHERE incident.service = ?
                  AND incident.status IN ({placeholders})
                  AND signal.{field} = ?
                  {scope_sql}
                ORDER BY incident.last_seen DESC
                """,
                (service, *open_states, value, *scope_params),
            ).fetchall()
            for row in rows:
                if not _within(row["last_seen"], event_time, suppression_seconds):
                    continue
                return row, field
        if identity["erp"]:
            rows = connection.execute(
                f"""
                SELECT DISTINCT incident.*
                FROM incident_signals AS signal
                JOIN incidents AS incident
                  ON incident.incident_id = signal.incident_id
                WHERE incident.service = ?
                  AND incident.status IN ({placeholders})
                  AND incident.fingerprint = ? AND signal.erp = ?
                  {scope_sql}
                ORDER BY incident.last_seen DESC
                """,
                (
                    service,
                    *open_states,
                    fingerprint,
                    identity["erp"],
                    *scope_params,
                ),
            ).fetchall()
            for row in rows:
                if not _within(row["last_seen"], event_time, suppression_seconds):
                    continue
                return row, "erp+fingerprint"
        rows = connection.execute(
            f"""
            SELECT DISTINCT incident.*
            FROM incidents AS incident
            JOIN incident_signals AS signal
              ON signal.incident_id = incident.incident_id
            WHERE incident.service = ?
              AND incident.status IN ({placeholders})
              AND incident.fingerprint = ?
              {scope_sql}
            ORDER BY incident.last_seen DESC
            """,
            (service, *open_states, fingerprint, *scope_params),
        ).fetchall()
        for row in rows:
            if row["fingerprint"] == fingerprint and _within(
                row["last_seen"], event_time, suppression_seconds
            ):
                return row, "fingerprint"
        return None, "new_incident"

    # ── incidents (fingerprint dedup + suppression window) ──────────────────────
    def upsert_incident(
        self,
        *,
        fingerprint: str,
        matched_rule: str,
        severity: str,
        eligibility: str,
        service: str,
        sample: dict[str, Any],
        suppression_seconds: int = 3600,
        open_states: tuple[str, ...] = ("open",),
    ) -> IncidentRecord:
        """Dedup by fingerprint within the suppression window (PRD FR-AUTO-004).

        If an incident with the same fingerprint is still within the window, bump its
        occurrence count / last_seen and return it (created=False). Otherwise create a
        new incident (created=True).
        """

        now = _now()
        with self._tx() as connection:
            row = connection.execute(
                """
                SELECT * FROM incidents
                WHERE fingerprint = ? AND status IN (%s)
                ORDER BY last_seen DESC LIMIT 1
                """
                % ",".join("?" for _ in open_states),
                (fingerprint, *open_states),
            ).fetchone()
            if row is not None and _within(row["last_seen"], now, suppression_seconds):
                connection.execute(
                    """
                    UPDATE incidents
                    SET occurrences = occurrences + 1, last_seen = ?, version = version + 1
                    WHERE incident_id = ?
                    """,
                    (now, row["incident_id"]),
                )
                return IncidentRecord(
                    incident_id=row["incident_id"],
                    fingerprint=fingerprint,
                    matched_rule=row["matched_rule"],
                    severity=row["severity"],
                    eligibility=row["eligibility"],
                    service=row["service"],
                    status=row["status"],
                    version=int(row["version"]) + 1,
                    occurrences=int(row["occurrences"]) + 1,
                    first_seen=row["first_seen"],
                    last_seen=now,
                    sample=json.loads(row["sample"]),
                    created=False,
                )
            incident_id = "incident-" + content_digest(
                {"fp": fingerprint, "first_seen": now, "service": service}
            )[:20]
            connection.execute(
                """
                INSERT INTO incidents(
                    incident_id, fingerprint, matched_rule, severity, eligibility,
                    service, status, version, occurrences, first_seen, last_seen, sample
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', 1, 1, ?, ?, ?)
                """,
                (
                    incident_id,
                    fingerprint,
                    matched_rule,
                    severity,
                    eligibility,
                    service,
                    now,
                    now,
                    _canonical(sample),
                ),
            )
            return IncidentRecord(
                incident_id=incident_id,
                fingerprint=fingerprint,
                matched_rule=matched_rule,
                severity=severity,
                eligibility=eligibility,
                service=service,
                status="open",
                version=1,
                occurrences=1,
                first_seen=now,
                last_seen=now,
                sample=sample,
                created=True,
            )

    def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        with self._tx() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                return None
            signals = self._list_incident_signals(connection, incident_id)
            record = dict(row)
            record["sample"] = json.loads(record["sample"])
            record["signals"] = list(signals)
        return record

    def get_incident_record(
        self, incident_id: str, *, created: bool = False
    ) -> IncidentRecord | None:
        with self._tx() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                return None
            signals = self._list_incident_signals(connection, incident_id)
        return _incident_from_row(row, created=created, signals=signals)

    def list_incident_signals(self, incident_id: str) -> list[dict[str, Any]]:
        with self._tx() as connection:
            return list(self._list_incident_signals(connection, incident_id))

    @staticmethod
    def _list_incident_signals(
        connection: sqlite3.Connection, incident_id: str
    ) -> tuple[dict[str, Any], ...]:
        rows = connection.execute(
            """
            SELECT rel.*, signal.source_id, signal.kind, signal.payload
            FROM incident_signals AS rel
            JOIN signals AS signal ON signal.signal_id = rel.signal_id
            WHERE rel.incident_id = ?
            ORDER BY CASE rel.role WHEN 'primary' THEN 0 ELSE 1 END,
                     rel.observed_at, rel.signal_id
            """,
            (incident_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return tuple(result)

    def mark_incident_signals(
        self,
        incident_id: str,
        *,
        processing_state: str,
        signal_ids: tuple[str, ...] = (),
    ) -> int:
        """Mark attached evidence without altering immutable raw signal payloads."""

        allowed = {
            "attached",
            "diagnosing",
            "processed",
            "ignored",
            "stale",
            "escalated",
        }
        if processing_state not in allowed:
            raise LoopStateError("invalid signal processing_state")
        if len(signal_ids) != len(set(signal_ids)):
            raise LoopStateError("signal_ids must be unique")
        with self._tx() as connection:
            incident = connection.execute(
                "SELECT status FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if incident is None:
                raise LoopStateError(f"unknown incident: {incident_id}")
            terminal_state = {
                "resolved": "processed",
                "duplicate": "ignored",
                "ignored": "ignored",
                "stale": "stale",
                "escalated": "escalated",
            }.get(str(incident["status"]))
            if terminal_state is not None and processing_state != terminal_state:
                raise LoopStateError(
                    f"incident {incident_id} is terminal and requires "
                    f"signal state {terminal_state}"
                )
            if signal_ids:
                placeholders = ",".join("?" for _ in signal_ids)
                count = connection.execute(
                    f"SELECT count(*) AS total FROM incident_signals "
                    f"WHERE incident_id = ? AND signal_id IN ({placeholders})",
                    (incident_id, *signal_ids),
                ).fetchone()["total"]
                if int(count) != len(signal_ids):
                    raise LoopStateError(
                        "one or more signals do not belong to the incident"
                    )
                cursor = connection.execute(
                    f"UPDATE incident_signals SET processing_state = ? "
                    f"WHERE incident_id = ? AND signal_id IN ({placeholders})",
                    (processing_state, incident_id, *signal_ids),
                )
            else:
                cursor = connection.execute(
                    "UPDATE incident_signals SET processing_state = ? "
                    "WHERE incident_id = ?",
                    (processing_state, incident_id),
                )
            return int(cursor.rowcount)

    def close_incident(
        self,
        incident_id: str,
        *,
        status: str,
        processing_state: str,
    ) -> None:
        """Atomically close an incident and all currently attached signals.

        Closing is idempotent for the same status and prevents later signals from
        being silently attached to an incident whose workflow already terminated.
        """

        allowed_statuses = {"duplicate", "ignored", "stale", "escalated"}
        if status not in allowed_statuses:
            raise LoopStateError("invalid terminal incident status")
        if processing_state not in {"ignored", "stale", "escalated"}:
            raise LoopStateError("invalid terminal signal processing_state")
        expected_processing_state = {
            "duplicate": "ignored",
            "ignored": "ignored",
            "stale": "stale",
            "escalated": "escalated",
        }[status]
        if processing_state != expected_processing_state:
            raise LoopStateError(
                "terminal incident status and signal processing_state disagree"
            )
        with self._tx(immediate=True) as connection:
            incident = connection.execute(
                "SELECT status FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if incident is None:
                raise LoopStateError(f"unknown incident: {incident_id}")
            current = str(incident["status"])
            if current not in {"open", status}:
                raise LoopStateError(
                    f"incident {incident_id} is already terminal: {current}"
                )
            if current != status:
                connection.execute(
                    "UPDATE incidents SET status = ?, version = version + 1 "
                    "WHERE incident_id = ?",
                    (status, incident_id),
                )
            connection.execute(
                "UPDATE incident_signals SET processing_state = ? "
                "WHERE incident_id = ?",
                (processing_state, incident_id),
            )

    def record_diagnosis_attempt(
        self,
        *,
        incident_id: str,
        attempt: int,
        hypothesis_digest: str,
        disposition: str,
        summary: str,
        evidence_refs: tuple[str, ...] = (),
    ) -> None:
        """Persist one meaningful diagnosis attempt; duplicate hypotheses fail closed."""

        if not 1 <= attempt <= 3:
            raise LoopStateError("diagnosis attempt must be in 1..3")
        if len(hypothesis_digest) != 64 or any(
            character not in "0123456789abcdef" for character in hypothesis_digest
        ):
            raise LoopStateError("hypothesis_digest must be lowercase SHA-256")
        if disposition not in {
            "reproduced",
            "duplicate",
            "stale_signal",
            "old_version_signal",
            "environment_blocked",
            "invalid_reproducer",
            "non_reproducible",
            "new_incident",
        }:
            raise LoopStateError("invalid reproduction disposition")
        if not summary.strip():
            raise LoopStateError("diagnosis summary cannot be blank")
        with self._tx() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO diagnosis_attempts(
                        incident_id, attempt, hypothesis_digest, disposition,
                        summary, evidence_refs, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident_id,
                        attempt,
                        hypothesis_digest,
                        disposition,
                        summary,
                        _canonical(evidence_refs),
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LoopStateError(
                    "diagnosis attempt is duplicate, exceeds budget, or references "
                    "an unknown incident"
                ) from exc

    def list_diagnosis_attempts(self, incident_id: str) -> list[dict[str, Any]]:
        with self._tx() as connection:
            rows = connection.execute(
                "SELECT * FROM diagnosis_attempts WHERE incident_id = ? ORDER BY attempt",
                (incident_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["evidence_refs"] = tuple(json.loads(item["evidence_refs"]))
            result.append(item)
        return result

    def record_resolution(
        self,
        *,
        incident_id: str,
        verified_candidate: str,
        deployed_version: str | None = None,
        deployed_at: str | None = None,
    ) -> None:
        verified_candidate = verified_candidate.strip()
        if not verified_candidate:
            raise LoopStateError("verified_candidate cannot be blank")
        with self._tx(immediate=True) as connection:
            incident = connection.execute(
                "SELECT status FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if incident is None:
                raise LoopStateError(f"unknown incident: {incident_id}")
            if incident["status"] not in {"open", "resolved"}:
                raise LoopStateError(
                    f"incident {incident_id} is already terminal: {incident['status']}"
                )
            existing = connection.execute(
                "SELECT * FROM incident_resolutions WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            if (
                incident["status"] == "resolved"
                and existing is not None
                and existing["verified_candidate"] != verified_candidate
            ):
                raise LoopStateError(
                    "resolved incident cannot be rebound to another candidate"
                )
            normalized_version = _clean_identifier(deployed_version)
            same_candidate = (
                existing is not None
                and existing["verified_candidate"] == verified_candidate
            )
            if same_candidate:
                assert existing is not None
                verified_at = existing["verified_at"]
                effective_version = (
                    normalized_version
                    if normalized_version is not None
                    else existing["deployed_version"]
                )
                effective_deployed_at = (
                    deployed_at
                    if deployed_at is not None
                    else existing["deployed_at"]
                )
            else:
                verified_at = _now()
                effective_version = normalized_version
                effective_deployed_at = deployed_at
            connection.execute(
                """
                INSERT INTO incident_resolutions(
                    incident_id, verified_candidate, deployed_version, verified_at, deployed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    verified_candidate = excluded.verified_candidate,
                    deployed_version = excluded.deployed_version,
                    verified_at = excluded.verified_at,
                    deployed_at = excluded.deployed_at
                """,
                (
                    incident_id,
                    verified_candidate,
                    effective_version,
                    verified_at,
                    effective_deployed_at,
                ),
            )
            if incident["status"] != "resolved":
                connection.execute(
                    "UPDATE incidents SET status = 'resolved', version = version + 1 "
                    "WHERE incident_id = ?",
                    (incident_id,),
                )
            connection.execute(
                "UPDATE incident_signals SET processing_state = 'processed' "
                "WHERE incident_id = ?",
                (incident_id,),
            )

    def get_resolution(self, incident_id: str) -> dict[str, Any] | None:
        with self._tx() as connection:
            row = connection.execute(
                "SELECT * FROM incident_resolutions WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
        return dict(row) if row else None

    # ── loop runs (idempotent create + CAS state machine + append-only events) ──
    def create_run(
        self, *, run_id: str, incident_id: str, request_digest: str, initial_state: str
    ) -> RunRecord:
        """Create a run idempotently.

        Same run_id + same request_digest -> reuse existing run (resume). Same run_id
        but a different request_digest -> integrity block (PRD §16 recovery rule).
        """

        now = _now()
        with self._tx() as connection:
            existing = connection.execute(
                "SELECT * FROM loop_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise LoopStateError(
                        f"run {run_id} exists with a different request digest — "
                        "integrity blocked"
                    )
                return _run_from_row(existing)
            connection.execute(
                """
                INSERT INTO loop_runs(
                    run_id, incident_id, state, version, attempt, request_digest, created_at
                ) VALUES (?, ?, ?, 1, 0, ?, ?)
                """,
                (run_id, incident_id, initial_state, request_digest, now),
            )
            return RunRecord(
                run_id=run_id,
                incident_id=incident_id,
                state=initial_state,
                version=1,
                attempt=0,
                request_digest=request_digest,
                created_at=now,
            )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._tx() as connection:
            row = connection.execute(
                "SELECT * FROM loop_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _run_from_row(row) if row else None

    def transition(
        self,
        run_id: str,
        *,
        expected_version: int,
        new_state: str,
        attempt: int | None = None,
    ) -> RunRecord:
        """Optimistic-locked state transition (PRD §9.2). Raises on version conflict."""

        with self._tx() as connection:
            row = connection.execute(
                "SELECT * FROM loop_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise LoopStateError(f"unknown run: {run_id}")
            if int(row["version"]) != expected_version:
                raise LoopStateError(
                    f"stale run version for {run_id}: expected {expected_version}, "
                    f"found {row['version']}"
                )
            new_attempt = row["attempt"] if attempt is None else attempt
            connection.execute(
                "UPDATE loop_runs SET state = ?, version = version + 1, attempt = ?"
                " WHERE run_id = ?",
                (new_state, new_attempt, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM loop_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return _run_from_row(updated)

    def append_event(self, run_id: str, *, kind: str, payload: dict[str, Any]) -> int:
        with self._tx() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            seq = int(row["m"]) + 1
            connection.execute(
                "INSERT INTO run_events(run_id, seq, kind, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, seq, kind, _canonical(payload), _now()),
            )
            return seq

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._tx() as connection:
            rows = connection.execute(
                "SELECT seq, kind, payload, created_at FROM run_events"
                " WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            events.append(item)
        return events

    # ── content-addressed artifacts ─────────────────────────────────────────────
    def put_artifact(
        self,
        *,
        artifact_type: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        incident_id: str | None = None,
    ) -> str:
        """Store an immutable artifact. Same digest -> reuse; same-name-diff-digest is
        impossible because the digest IS the identity (PRD §16)."""

        digest = content_digest({"t": artifact_type, "p": payload})
        with self._tx() as connection:
            existing = connection.execute(
                "SELECT payload FROM artifacts WHERE digest = ?", (digest,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO artifacts(
                        digest, artifact_type, run_id, incident_id, payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        digest,
                        artifact_type,
                        run_id,
                        incident_id,
                        _canonical(payload),
                        _now(),
                    ),
                )
        return digest

    def get_artifact(self, digest: str) -> dict[str, Any] | None:
        with self._tx() as connection:
            row = connection.execute(
                "SELECT artifact_type, run_id, incident_id, payload, created_at"
                " FROM artifacts WHERE digest = ?",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["payload"] = json.loads(record["payload"])
        record["digest"] = digest
        return record

    # ── outbox (idempotent external side-effects) ───────────────────────────────
    def enqueue_outbox(
        self,
        *,
        idempotency_key: str,
        action_type: str,
        request_digest: str,
        payload: dict[str, Any],
    ) -> bool:
        """Record an intent to perform an external side-effect. Idempotent by key.

        Returns True if newly enqueued, False if this key was already recorded
        (so a crashed-then-resumed run never double-fires a PR/commit/push).
        """

        now = _now()
        with self._tx() as connection:
            existing = connection.execute(
                "SELECT request_digest FROM outbox WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise LoopStateError(
                        f"outbox key {idempotency_key} exists with a different request "
                        "digest — refusing to reuse"
                    )
                return False
            connection.execute(
                """
                INSERT INTO outbox(
                    idempotency_key, action_type, request_digest, payload,
                    status, external_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?)
                """,
                (
                    idempotency_key,
                    action_type,
                    request_digest,
                    _canonical(payload),
                    now,
                    now,
                ),
            )
            return True

    def mark_outbox(
        self, idempotency_key: str, *, status: str, external_id: str | None = None
    ) -> None:
        with self._tx() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET status = ?, external_id = ?, updated_at = ?"
                " WHERE idempotency_key = ?",
                (status, external_id, _now(), idempotency_key),
            )
            if cursor.rowcount == 0:
                raise LoopStateError(f"unknown outbox key: {idempotency_key}")

    def get_outbox(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._tx() as connection:
            row = connection.execute(
                "SELECT * FROM outbox WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["payload"] = json.loads(record["payload"])
        return record

    def list_outbox(
        self, *, status: str | None = None, action_type: str | None = None
    ) -> list[dict[str, Any]]:
        """List outbox intents (oldest first), optionally filtered by status/action.

        Lets a worker drain pending work the scheduler enqueued, keeping agent
        invocation out of the cron path (PRD §16 "Cron 只负责入队").
        """

        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if action_type is not None:
            clauses.append("action_type = ?")
            params.append(action_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._tx() as connection:
            rows = connection.execute(
                "SELECT * FROM outbox" + where + " ORDER BY created_at ASC, rowid ASC",
                params,
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["payload"] = json.loads(record["payload"])
            records.append(record)
        return records

    def reset_cursor(self, source_id: str) -> None:
        """Drop a source's incremental cursor so the next scan reads from the start.

        Used by the daily full-sweep job to re-examine chronic/long-tail anomalies
        that an incremental cursor would skip.
        """

        with self._tx() as connection:
            connection.execute(
                "DELETE FROM source_cursors WHERE source_id = ?", (source_id,)
            )

    @staticmethod
    def operation_key(
        *, run_id: str, cycle: int, stage: str, input_digest: str, component_version: str
    ) -> str:
        """Deterministic stage idempotency key (PRD §16)."""

        return content_digest(
            {
                "run_id": run_id,
                "cycle": cycle,
                "stage": stage,
                "input_digest": input_digest,
                "component_version": component_version,
            }
        )


def _within(previous_iso: str, now_iso: str, seconds: int) -> bool:
    try:
        previous = datetime.fromisoformat(previous_iso)
        now = datetime.fromisoformat(now_iso)
    except ValueError:
        return False
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return abs((now - previous).total_seconds()) <= seconds


def _normalize_timestamp(value: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _clean_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _higher_severity(current: str, incoming: str) -> str:
    rank = {"low": 0, "medium": 1, "high": 2}
    if current not in rank or incoming not in rank:
        return current
    return incoming if rank[incoming] > rank[current] else current


def _more_restrictive_eligibility(current: str, incoming: str) -> str:
    rank = {"auto_fix_eligible": 0, "diagnose_only": 1, "record_only": 2}
    if current not in rank or incoming not in rank:
        return current
    return incoming if rank[incoming] > rank[current] else current


def _incident_from_row(
    row: sqlite3.Row,
    *,
    created: bool,
    signals: tuple[dict[str, Any], ...] = (),
) -> IncidentRecord:
    return IncidentRecord(
        incident_id=row["incident_id"],
        fingerprint=row["fingerprint"],
        matched_rule=row["matched_rule"],
        severity=row["severity"],
        eligibility=row["eligibility"],
        service=row["service"],
        status=row["status"],
        version=int(row["version"]),
        occurrences=int(row["occurrences"]),
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        sample=json.loads(row["sample"]),
        created=created,
        signals=signals,
    )


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        incident_id=row["incident_id"],
        state=row["state"],
        version=int(row["version"]),
        attempt=int(row["attempt"]),
        request_digest=row["request_digest"],
        created_at=row["created_at"],
    )


__all__ = [
    "IncidentRecord",
    "LoopStateError",
    "LoopStateStore",
    "RunRecord",
    "SignalIngestResult",
    "content_digest",
]
