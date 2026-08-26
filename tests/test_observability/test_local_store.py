from __future__ import annotations

from pathlib import Path

import pytest

from core.observability import (
    CCBDebugLogImporter,
    ExecutionWindow,
    LocalObservabilityStore,
    SQLiteBehaviorEvidenceProvider,
    SQLiteLogEvidenceProvider,
    SQLiteTraceEvidenceProvider,
    normalized_input_digest,
)
from core.verification import Variant
from core.verification.providers import EvidenceCollectionContext


DIGEST = "a" * 64
CONTROL_DIGEST = "b" * 64
POLICY_DIGEST = "c" * 64
SKILL_DIGESTS = {"checkout": "d" * 64}
INPUT_PAYLOAD = {"prompt": "reproduce checkout timeout", "seed": 7}
INPUT_DIGEST = normalized_input_digest(INPUT_PAYLOAD)


def _attr(key: str, value):
    kind = "boolValue" if isinstance(value, bool) else "stringValue"
    if isinstance(value, int):
        kind = "intValue"
        value = str(value)
    return {"key": key, "value": {kind: value}}


def _resource(run_id: str, scenario: str, variant: str):
    return {
        "attributes": [
            _attr("service.name", "claude-code"),
            _attr("service.version", "sha-one"),
            _attr("verification.run_id", run_id),
            _attr("verification.scenario_id", scenario),
            _attr("verification.variant", variant),
            _attr("verification.input_digest", INPUT_DIGEST),
        ]
    }


def _trace_payload(run_id: str, scenario: str, variant: str, trace_id: str):
    root_span_id = "11" * 8
    llm_span_id = "22" * 8
    return {
        "resourceSpans": [
            {
                "resource": _resource(run_id, scenario, variant),
                "scopeSpans": [
                    {
                        "scope": {"name": "ccb"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": root_span_id,
                                "name": "claude_code.interaction",
                                "startTimeUnixNano": "100",
                                "endTimeUnixNano": "900",
                                "attributes": [_attr("session.id", f"session-{variant}")],
                                "events": [],
                            },
                            {
                                "traceId": trace_id,
                                "spanId": llm_span_id,
                                "parentSpanId": root_span_id,
                                "name": "claude_code.llm_request",
                                "startTimeUnixNano": "200",
                                "endTimeUnixNano": "800",
                                "attributes": [
                                    _attr("span.type", "llm_request"),
                                    _attr("session.id", f"session-{variant}"),
                                    _attr("model", "model-a"),
                                    _attr("input_tokens", 42),
                                    _attr("success", True),
                                ],
                                "events": [],
                                "status": {"code": "STATUS_CODE_OK"},
                            },
                        ],
                    }
                ],
            }
        ]
    }


def _log_payload(run_id: str, scenario: str, variant: str, trace_id: str):
    return {
        "resourceLogs": [
            {
                "resource": _resource(run_id, scenario, variant),
                "scopeLogs": [
                    {
                        "scope": {"name": "ccb"},
                        "logRecords": [
                            {
                                "timeUnixNano": "400",
                                "observedTimeUnixNano": "401",
                                "body": {"stringValue": "claude_code.api_request"},
                                "attributes": [
                                    _attr("session.id", f"session-{variant}"),
                                    _attr("event.name", "api_request"),
                                    _attr("model", "model-a"),
                                    _attr("input_tokens", 10),
                                    _attr("cache_read_tokens", 20),
                                    _attr("cache_creation_tokens", 12),
                                ],
                            },
                            {
                                "timeUnixNano": "500",
                                "observedTimeUnixNano": "501",
                                "traceId": trace_id,
                                "spanId": "22" * 8,
                                "body": {"stringValue": "TimeoutError request 123 failed"},
                                "attributes": [
                                    _attr("session.id", f"session-{variant}"),
                                    _attr("event.name", "api_error"),
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _window(run_id: str, scenario: str, variant: str, trace_id: str):
    return ExecutionWindow(
        run_id=run_id,
        cycle=1,
        scenario_id=scenario,
        variant=variant,
        input_digest=INPUT_DIGEST,
        input_payload=INPUT_PAYLOAD,
        collection_id=f"collection-{variant}",
        control_ref="control",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        skill_digests=SKILL_DIGESTS,
        started_at_ns=1,
        ended_at_ns=1000,
        collection_complete=True,
        trace_id=trace_id,
        session_id=f"session-{variant}",
        finished=True,
        outcome="failure" if variant == "control" else "success",
        payload={"status": 500 if variant == "control" else 200},
        model="model-a",
        tool_calls=(
            {
                "sequence": 0,
                "tool_name": "Bash",
                "input_digest": "1" * 64,
                "output_digest": "2" * 64,
                "outcome": "success",
            },
        ),
    )


def _context() -> EvidenceCollectionContext:
    return EvidenceCollectionContext(
        run_id="run-1",
        cycle=1,
        control_ref="control",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate",
        candidate_digest=DIGEST,
        policy_digest=POLICY_DIGEST,
        skill_names=("checkout",),
        skill_digests=SKILL_DIGESTS,
        scenario_ids=("checkout:case",),
    )


async def test_otlp_ingestion_and_all_three_providers(tmp_path: Path) -> None:
    database = tmp_path / "observability.sqlite3"
    store = LocalObservabilityStore(database)
    for variant, trace_id in (
        ("control", "aa" * 16),
        ("candidate", "bb" * 16),
    ):
        assert store.ingest_otlp_traces(
            _trace_payload("run-1", "checkout:case", variant, trace_id)
        ) == 2
        assert store.ingest_otlp_logs(
            _log_payload("run-1", "checkout:case", variant, trace_id)
        ) == 2
        store.record_execution(_window("run-1", "checkout:case", variant, trace_id))

    trace = await SQLiteTraceEvidenceProvider(str(database)).collect_trace(_context())
    assert trace.collection_complete is True
    assert trace.observations[0].trace_id == "bb" * 16
    assert trace.observations[0].session_id == "session-candidate"
    assert trace.observations[0].actual_model == "model-a"
    assert trace.observations[0].input_tokens == 42
    assert trace.observations[0].fallback_used is None
    assert trace.observations[0].finished is True

    logs = await SQLiteLogEvidenceProvider(str(database)).collect_logs(_context())
    assert logs.collection_complete is True
    assert {item.variant for item in logs.observations} == {
        Variant.CONTROL,
        Variant.CANDIDATE,
    }
    assert {item.error_type for item in logs.observations} == {"TimeoutError"}
    assert all(item.message_template == "TimeoutError request <n> failed" for item in logs.observations)
    queried = store.search_logs(
        run_id="run-1",
        scenario_id="checkout:case",
        variant="candidate",
        service_name="claude-code",
        level="ERROR",
        start_time_ns=500,
        end_time_ns=500,
    )
    assert len(queried) == 1
    assert queried[0]["trace_id"] == "bb" * 16

    behavior = await SQLiteBehaviorEvidenceProvider(str(database)).collect_behavior(
        _context()
    )
    assert behavior.collection_complete is True
    assert len(behavior.observations) == 2
    assert behavior.observations[1].trace_id == "bb" * 16


async def test_missing_explicit_finished_remains_unknown(tmp_path: Path) -> None:
    database = tmp_path / "observability.sqlite3"
    store = LocalObservabilityStore(database)
    for variant, trace_id in (
        ("control", "aa" * 16),
        ("candidate", "bb" * 16),
    ):
        store.ingest_otlp_traces(
            _trace_payload("run-1", "checkout:case", variant, trace_id)
        )
        window = _window("run-1", "checkout:case", variant, trace_id)
        if variant == "candidate":
            window = ExecutionWindow(**{**window.__dict__, "finished": None})
        store.record_execution(window)

    trace = await SQLiteTraceEvidenceProvider(str(database)).collect_trace(_context())
    assert trace.observations[0].finished is None


def test_ccb_debug_import_is_incremental_and_keeps_session(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    debug_dir = tmp_path / "debug"
    debug_dir.mkdir()
    path = debug_dir / "session-123.txt"
    path.write_text(
        "2026-08-24T10:00:00.000Z [ERROR] ToolException request 42 failed\n",
        encoding="utf-8",
    )
    importer = CCBDebugLogImporter(store)
    assert importer.import_directory(debug_dir) == 1
    assert importer.import_directory(debug_dir) == 0
    rows = store.search_logs(session_id="session-123", level="ERROR")
    assert len(rows) == 1
    assert rows[0]["error_type"] == "ToolException"
    assert rows[0]["message_template"] == "ToolException request <n> failed"


def test_ccb_debug_import_restarts_after_in_place_truncation(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    path = tmp_path / "session-rotation.txt"
    path.write_text(
        "2026-08-24T10:00:00.000Z [INFO] first long message\n",
        encoding="utf-8",
    )
    importer = CCBDebugLogImporter(store)
    assert importer.import_file(path) == 1

    path.write_text(
        "2026-08-24T10:01:00.000Z [ERROR] NewError\n",
        encoding="utf-8",
    )
    assert importer.import_file(path) == 1
    assert len(store.search_logs(session_id="session-rotation")) == 2


def test_failed_tool_result_is_stored_as_error(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    payload = {
        "resourceLogs": [
            {
                "resource": _resource("run-1", "checkout:case", "candidate"),
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "500",
                                "severityText": "INFO",
                                "body": {"stringValue": "claude_code.tool_result"},
                                "attributes": [
                                    _attr("event.name", "tool_result"),
                                    _attr("success", "false"),
                                    _attr("error", "TimeoutError request 99 failed"),
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    assert store.ingest_otlp_logs(payload) == 1
    rows = store.search_logs(level="ERROR")
    assert rows[0]["error_type"] == "TimeoutError"
    assert rows[0]["message_template"] == "TimeoutError request <n> failed"


def test_log_search_rejects_invalid_window_and_variant(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    with pytest.raises(Exception, match="variant"):
        store.search_logs(variant="production")
    with pytest.raises(Exception, match="不能早于"):
        store.search_logs(start_time_ns=2, end_time_ns=1)


def test_invalid_otlp_trace_identifier_is_rejected(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    payload = _trace_payload("run-1", "checkout:case", "candidate", "not-a-trace")
    with pytest.raises(Exception, match="traceId/spanId"):
        store.ingest_otlp_traces(payload)
