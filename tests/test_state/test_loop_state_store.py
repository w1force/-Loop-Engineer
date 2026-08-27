"""LoopStateStore: cursors, dedup, CAS state machine, content-addressed artifacts, outbox."""

from __future__ import annotations

from pathlib import Path

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
