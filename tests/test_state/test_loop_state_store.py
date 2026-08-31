"""LoopStateStore: cursors, dedup, CAS state machine, content-addressed artifacts, outbox."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3

import pytest

from core.state import LoopStateError, LoopStateStore


def _store(tmp_path: Path) -> LoopStateStore:
    return LoopStateStore(tmp_path / "state.db")


def test_cursor_roundtrip(tmp_path: Path):
    st = _store(tmp_path)
    assert st.get_cursor("src") is None
    st.set_cursor("src", path="/a/b.jsonl", inode=123, byte_offset=456)
    cur = st.get_cursor("src")
    assert cur["inode"] == 123 and cur["byte_offset"] == 456
    st.set_cursor("src", path="/a/b.jsonl", inode=123, byte_offset=789)
    assert st.get_cursor("src")["byte_offset"] == 789


def test_incident_dedup_and_suppression(tmp_path: Path):
    st = _store(tmp_path)
    kw = dict(
        fingerprint="f" * 64,
        matched_rule="r",
        severity="high",
        eligibility="auto_fix_eligible",
        service="ccb",
        sample={"x": 1},
    )
    a = st.upsert_incident(**kw)
    b = st.upsert_incident(**kw)  # within suppression window -> deduped
    assert a.created and not b.created
    assert a.incident_id == b.incident_id
    assert b.occurrences == 2
    # zero suppression window -> a fresh occurrence creates a new incident
    c = st.upsert_incident(**{**kw, "fingerprint": "e" * 64}, suppression_seconds=0)
    d = st.upsert_incident(**{**kw, "fingerprint": "e" * 64}, suppression_seconds=0)
    assert c.created and d.created
    assert c.incident_id != d.incident_id


def test_signal_idempotent(tmp_path: Path):
    st = _store(tmp_path)
    kw = dict(
        signal_id="sig-1",
        fingerprint="f" * 64,
        source_id="src",
        kind="run_error",
        observed_at="2026-01-01T00:00:00+00:00",
        payload={"a": 1},
    )
    assert st.record_signal(**kw) is True
    assert st.record_signal(**kw) is False  # same signal_id ignored


def test_ingest_groups_different_errors_by_trace_and_rescan_is_idempotent(
    tmp_path: Path,
) -> None:
    st = _store(tmp_path)

    def ingest(signal_id: str, fingerprint: str, observed_at: str):
        return st.ingest_signal(
            signal_id=signal_id,
            fingerprint=fingerprint,
            source_id="logs",
            kind="error",
            observed_at=observed_at,
            payload={"signal_id": signal_id, "message": signal_id},
            matched_rule="service.error",
            severity="high",
            eligibility="auto_fix_eligible",
            service="orders",
            trace_id="trace-1",
        )

    first = ingest("signal-1", "a" * 64, "2026-01-01T00:00:00Z")
    second = ingest("signal-2", "b" * 64, "2026-01-01T00:00:10Z")
    duplicate = ingest("signal-2", "b" * 64, "2026-01-01T00:00:10Z")

    assert first.incident.incident_id == second.incident.incident_id
    assert second.role == "secondary" and second.correlation_reason == "trace_id"
    assert duplicate.signal_created is False
    assert st.get_incident(first.incident.incident_id)["occurrences"] == 2


def test_related_signal_conservatively_tightens_incident_policy(tmp_path: Path) -> None:
    st = _store(tmp_path)
    common = dict(
        source_id="logs",
        kind="error",
        observed_at="2026-01-01T00:00:00Z",
        payload={"message": "same request"},
        matched_rule="service.error",
        service="orders",
        trace_id="trace-policy",
        environment="prod",
        deployment_version="v1",
    )
    first = st.ingest_signal(
        signal_id="policy-1",
        fingerprint="1" * 64,
        severity="medium",
        eligibility="auto_fix_eligible",
        **common,
    )
    second = st.ingest_signal(
        signal_id="policy-2",
        fingerprint="2" * 64,
        severity="high",
        eligibility="diagnose_only",
        **common,
    )

    assert first.incident.incident_id == second.incident.incident_id
    incident = st.get_incident(first.incident.incident_id)
    assert incident["severity"] == "high"
    assert incident["eligibility"] == "diagnose_only"


def test_ingest_uses_event_time_not_scan_time_for_suppression(tmp_path: Path) -> None:
    st = _store(tmp_path)
    common = dict(
        fingerprint="c" * 64,
        source_id="logs",
        kind="error",
        payload={"message": "same shape"},
        matched_rule="service.error",
        severity="high",
        eligibility="auto_fix_eligible",
        service="orders",
        suppression_seconds=60,
    )
    old = st.ingest_signal(
        signal_id="old", observed_at="2026-01-01T00:00:00Z", **common
    )
    recent = st.ingest_signal(
        signal_id="recent", observed_at="2026-01-01T02:00:00Z", **common
    )
    assert old.incident.incident_id != recent.incident.incident_id


def test_incident_correlation_isolated_by_environment_version_and_null_scope(
    tmp_path: Path,
) -> None:
    st = _store(tmp_path)

    def ingest(signal_id: str, environment, deployment_version):
        return st.ingest_signal(
            signal_id=signal_id,
            fingerprint="c" * 64,
            source_id="logs",
            kind="error",
            observed_at="2026-01-01T00:00:00Z",
            payload={"message": "same failure"},
            matched_rule="service.error",
            severity="high",
            eligibility="auto_fix_eligible",
            service="orders",
            trace_id="trace-shared",
            environment=environment,
            deployment_version=deployment_version,
        )

    prod_v1 = ingest("prod-v1", "prod", "v1")
    staging_v1 = ingest("staging-v1", "staging", "v1")
    prod_v2 = ingest("prod-v2", "prod", "v2")
    unknown_1 = ingest("unknown-1", None, None)
    unknown_2 = ingest("unknown-2", None, None)

    assert len(
        {
            prod_v1.incident.incident_id,
            staging_v1.incident.incident_id,
            prod_v2.incident.incident_id,
            unknown_1.incident.incident_id,
        }
    ) == 4
    assert unknown_2.incident.incident_id == unknown_1.incident.incident_id


def test_concurrent_signal_ingest_is_idempotent(tmp_path: Path) -> None:
    st = _store(tmp_path)

    def ingest(_index: int):
        return st.ingest_signal(
            signal_id="same-signal",
            fingerprint="9" * 64,
            source_id="logs",
            kind="error",
            observed_at="2026-01-01T00:00:00Z",
            payload={"message": "boom"},
            matched_rule="service.error",
            severity="high",
            eligibility="auto_fix_eligible",
            service="orders",
            request_id="request-1",
            environment="prod",
            deployment_version="v1",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(ingest, range(16)))

    assert sum(result.signal_created for result in results) == 1
    incident_ids = {result.incident.incident_id for result in results}
    assert len(incident_ids) == 1
    incident = st.get_incident(incident_ids.pop())
    assert incident["occurrences"] == 1
    assert len(incident["signals"]) == 1


def test_partial_legacy_incident_schema_migrates_and_restarts(tmp_path: Path) -> None:
    database = tmp_path / "legacy-state.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE signals (
                signal_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                source_id TEXT NOT NULL, kind TEXT NOT NULL,
                observed_at TEXT NOT NULL, payload TEXT NOT NULL,
                incident_id TEXT
            );
            CREATE TABLE incidents (
                incident_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                matched_rule TEXT NOT NULL, severity TEXT NOT NULL,
                eligibility TEXT NOT NULL, service TEXT NOT NULL,
                status TEXT NOT NULL, version INTEGER NOT NULL,
                occurrences INTEGER NOT NULL, first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL, sample TEXT NOT NULL
            );
            CREATE TABLE incident_signals (
                signal_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
                role TEXT NOT NULL, fingerprint TEXT NOT NULL,
                observed_at TEXT NOT NULL, trace_id TEXT, request_id TEXT,
                run_id TEXT, session_id TEXT, erp TEXT, environment TEXT,
                correlation_reason TEXT NOT NULL,
                processing_state TEXT NOT NULL DEFAULT 'attached',
                attached_at TEXT NOT NULL
            );
            """
        )
        payload = {
            "message": "legacy",
            "environment": "prod",
            "deployment_version": "v1",
        }
        connection.execute(
            "INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "incident-legacy",
                "a" * 64,
                "service.error",
                "high",
                "auto_fix_eligible",
                "orders",
                "open",
                1,
                1,
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                json.dumps(payload),
            ),
        )
        connection.execute(
            "INSERT INTO signals VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "signal-legacy",
                "a" * 64,
                "logs",
                "error",
                "2026-01-01T00:00:00Z",
                json.dumps(payload),
                "incident-legacy",
            ),
        )
        connection.execute(
            "INSERT INTO incident_signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "signal-legacy",
                "incident-legacy",
                "primary",
                "a" * 64,
                "2026-01-01T00:00:00Z",
                None,
                None,
                None,
                None,
                None,
                "prod",
                "migration",
                "attached",
                "2026-01-01T00:00:00Z",
            ),
        )

    store = LoopStateStore(database)
    LoopStateStore(database)  # migration is restart-idempotent
    signals = store.list_incident_signals("incident-legacy")
    assert signals[0]["environment"] == "prod"
    assert signals[0]["deployment_version"] == "v1"


def test_resolution_marks_related_signals_processed(tmp_path: Path) -> None:
    st = _store(tmp_path)
    ingested = st.ingest_signal(
        signal_id="signal-1",
        fingerprint="d" * 64,
        source_id="logs",
        kind="error",
        observed_at="2026-01-01T00:00:00Z",
        payload={"message": "boom"},
        matched_rule="service.error",
        severity="high",
        eligibility="auto_fix_eligible",
        service="orders",
    )
    assert st.mark_incident_signals(
        ingested.incident.incident_id, processing_state="diagnosing"
    ) == 1
    st.record_resolution(
        incident_id=ingested.incident.incident_id,
        verified_candidate="candidate-sha",
    )
    incident = st.get_incident(ingested.incident.incident_id)
    assert incident["status"] == "resolved"
    assert incident["signals"][0]["processing_state"] == "processed"


def test_terminal_incident_transitions_are_idempotent_and_irreversible(
    tmp_path: Path,
) -> None:
    st = _store(tmp_path)

    def new_incident(signal_id: str):
        return st.ingest_signal(
            signal_id=signal_id,
            fingerprint=("a" if signal_id == "closed" else "b") * 64,
            source_id="logs",
            kind="error",
            observed_at="2026-01-01T00:00:00Z",
            payload={"message": "boom"},
            matched_rule="service.error",
            severity="high",
            eligibility="auto_fix_eligible",
            service="orders",
        ).incident.incident_id

    closed = new_incident("closed")
    st.close_incident(closed, status="duplicate", processing_state="ignored")
    first_version = st.get_incident(closed)["version"]
    st.close_incident(closed, status="duplicate", processing_state="ignored")
    assert st.get_incident(closed)["version"] == first_version
    with pytest.raises(LoopStateError, match="terminal"):
        st.close_incident(closed, status="escalated", processing_state="escalated")
    with pytest.raises(LoopStateError, match="terminal"):
        st.record_resolution(incident_id=closed, verified_candidate="candidate-a")
    with pytest.raises(LoopStateError, match="disagree"):
        st.close_incident(closed, status="duplicate", processing_state="stale")

    resolved = new_incident("resolved")
    st.record_resolution(incident_id=resolved, verified_candidate="candidate-a")
    resolved_version = st.get_incident(resolved)["version"]
    st.record_resolution(incident_id=resolved, verified_candidate="candidate-a")
    assert st.get_incident(resolved)["version"] == resolved_version
    with pytest.raises(LoopStateError, match="rebound"):
        st.record_resolution(incident_id=resolved, verified_candidate="candidate-b")
    with pytest.raises(LoopStateError, match="terminal"):
        st.close_incident(resolved, status="stale", processing_state="stale")
    with pytest.raises(LoopStateError, match="terminal"):
        st.mark_incident_signals(resolved, processing_state="diagnosing")


def test_duplicate_diagnosis_hypothesis_is_rejected(tmp_path: Path) -> None:
    st = _store(tmp_path)
    ingested = st.ingest_signal(
        signal_id="signal-1",
        fingerprint="e" * 64,
        source_id="logs",
        kind="error",
        observed_at="2026-01-01T00:00:00Z",
        payload={"message": "boom"},
        matched_rule="service.error",
        severity="high",
        eligibility="auto_fix_eligible",
        service="orders",
    )
    kwargs = dict(
        incident_id=ingested.incident.incident_id,
        hypothesis_digest="f" * 64,
        disposition="non_reproducible",
        summary="control did not reproduce",
    )
    st.record_diagnosis_attempt(attempt=1, **kwargs)
    with pytest.raises(LoopStateError, match="duplicate"):
        st.record_diagnosis_attempt(attempt=2, **kwargs)


def test_run_create_idempotent_and_integrity_block(tmp_path: Path):
    st = _store(tmp_path)
    r1 = st.create_run(
        run_id="run-1", incident_id="inc-1", request_digest="d1", initial_state="INGESTED"
    )
    assert r1.version == 1 and r1.state == "INGESTED"
    # same run_id + same digest -> reuse (resume)
    r2 = st.create_run(
        run_id="run-1", incident_id="inc-1", request_digest="d1", initial_state="INGESTED"
    )
    assert r2.run_id == "run-1"
    # same run_id + different digest -> integrity block
    with pytest.raises(LoopStateError):
        st.create_run(
            run_id="run-1", incident_id="inc-1", request_digest="d2", initial_state="X"
        )


def test_transition_is_cas_locked(tmp_path: Path):
    st = _store(tmp_path)
    st.create_run(
        run_id="run-1", incident_id="inc-1", request_digest="d", initial_state="INGESTED"
    )
    moved = st.transition("run-1", expected_version=1, new_state="DIAGNOSING", attempt=1)
    assert moved.state == "DIAGNOSING" and moved.version == 2 and moved.attempt == 1
    # stale version rejected
    with pytest.raises(LoopStateError):
        st.transition("run-1", expected_version=1, new_state="REPAIRING")


def test_events_append_only_and_ordered(tmp_path: Path):
    st = _store(tmp_path)
    st.create_run(
        run_id="run-1", incident_id="inc-1", request_digest="d", initial_state="I"
    )
    assert st.append_event("run-1", kind="a", payload={}) == 1
    assert st.append_event("run-1", kind="b", payload={"k": 2}) == 2
    events = st.list_events("run-1")
    assert [e["kind"] for e in events] == ["a", "b"]
    assert events[1]["payload"] == {"k": 2}


def test_artifact_is_content_addressed(tmp_path: Path):
    st = _store(tmp_path)
    d1 = st.put_artifact(artifact_type="incident", payload={"a": 1})
    d2 = st.put_artifact(artifact_type="incident", payload={"a": 1})
    d3 = st.put_artifact(artifact_type="incident", payload={"a": 2})
    assert d1 == d2 and d1 != d3  # same content -> same digest; different -> different
    assert st.get_artifact(d1)["payload"] == {"a": 1}
    assert st.get_artifact("0" * 64) is None


def test_outbox_idempotent(tmp_path: Path):
    st = _store(tmp_path)
    assert st.enqueue_outbox(
        idempotency_key="pr:run-1", action_type="create_pr", request_digest="d", payload={}
    ) is True
    # same key -> not re-enqueued (crash-safe: never double-fires the PR)
    assert st.enqueue_outbox(
        idempotency_key="pr:run-1", action_type="create_pr", request_digest="d", payload={}
    ) is False
    # same key, different request -> refuse
    with pytest.raises(LoopStateError):
        st.enqueue_outbox(
            idempotency_key="pr:run-1", action_type="create_pr", request_digest="d2", payload={}
        )
    st.mark_outbox("pr:run-1", status="done", external_id="PR#5")
    assert st.get_outbox("pr:run-1")["external_id"] == "PR#5"


def test_operation_key_is_deterministic(tmp_path: Path):
    a = LoopStateStore.operation_key(
        run_id="r", cycle=1, stage="repair", input_digest="x", component_version="v1"
    )
    b = LoopStateStore.operation_key(
        run_id="r", cycle=1, stage="repair", input_digest="x", component_version="v1"
    )
    c = LoopStateStore.operation_key(
        run_id="r", cycle=2, stage="repair", input_digest="x", component_version="v1"
    )
    assert a == b and a != c
