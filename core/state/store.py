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


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    incident_id: str
    state: str
    version: int
    attempt: int
    request_digest: str
    created_at: str


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
    def _tx(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
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
            connection.commit()
        finally:
            connection.close()

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
        record = dict(row)
        record["sample"] = json.loads(record["sample"])
        return record

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
    return (now - previous).total_seconds() <= seconds


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
    "content_digest",
]
