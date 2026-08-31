from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import time

import pytest

from core.observability import (
    CCBDebugLogImporter,
    ExecutionWindow,
    LocalObservabilityStore,
    OtlpFlushBarrier,
    SQLiteBehaviorEvidenceProvider,
    SQLiteLogEvidenceProvider,
    SQLiteTraceEvidenceProvider,
    normalized_input_digest,
)
from core.verification import (
    GateStatus,
    ReplayEvidenceManifest,
    ReplayWindowBinding,
    TraceGateSpec,
    Variant,
)
from core.verification.gates import evaluate_trace_gate
from core.verification.providers import EvidenceCollectionContext


DIGEST = "a" * 64
CONTROL_DIGEST = "b" * 64
POLICY_DIGEST = "c" * 64
SKILL_DIGESTS = {"checkout": "d" * 64}
INPUT_PAYLOAD = {"prompt": "reproduce checkout timeout", "seed": 7}
INPUT_DIGEST = normalized_input_digest(INPUT_PAYLOAD)
ORACLE_DIGEST = "e" * 64
RESULT_DIGEST = "f" * 64


def _attr(key: str, value):
    kind = "boolValue" if isinstance(value, bool) else "stringValue"
    if isinstance(value, int) and not isinstance(value, bool):
        kind = "intValue"
        value = str(value)
    return {"key": key, "value": {kind: value}}


def _resource(run_id: str, scenario: str, variant: str):
    return {
        "attributes": [
            _attr("service.name", "claude-code"),
            _attr("service.version", "sha-one"),
            _attr("verification.run_id", run_id),
            _attr("verification.cycle", 1),
            _attr("verification.scenario_id", scenario),
            _attr("verification.variant", variant),
            _attr("verification.input_digest", INPUT_DIGEST),
            _attr("verification.collection_id", f"collection-{variant}"),
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
                                    _attr("fallback_used", False),
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
        oracle_digest=ORACLE_DIGEST,
        result_sha256=RESULT_DIGEST,
    )


def _context(store: LocalObservabilityStore) -> EvidenceCollectionContext:
    bindings = []
    for row in store.execution_windows("run-1", 1):
        try:
            barrier_digest = store.otlp_barrier_digest(row)
        except Exception:
            barrier_digest = "0" * 64
        bindings.append(
            ReplayWindowBinding(
                scenario_id=row["scenario_id"],
                variant=Variant(row["variant"]),
                input_digest=row["input_digest"],
                collection_id=row["collection_id"],
                otlp_barrier_digest=barrier_digest,
                oracle_digest=ORACLE_DIGEST,
                result_sha256=RESULT_DIGEST,
            )
        )
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
        replay_manifest=ReplayEvidenceManifest(windows=tuple(bindings)),
    )


def _record_and_seal(store: LocalObservabilityStore, window: ExecutionWindow) -> None:
    store.record_execution(window)
    store.record_otlp_flush_barrier(
        OtlpFlushBarrier(
            flush_id=f"flush-{window.collection_id}",
            collection_id=window.collection_id,
            run_id=window.run_id,
            cycle=window.cycle,
            scenario_id=window.scenario_id,
            variant=window.variant,
            input_digest=window.input_digest,
            signals=("traces", "logs"),
            flush_started_at_ns=window.ended_at_ns,
            flush_completed_at_ns=window.ended_at_ns,
            deadline_ns=time.time_ns() + 10_000_000_000,
        )
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
        _record_and_seal(
            store, _window("run-1", "checkout:case", variant, trace_id)
        )

    trace = await SQLiteTraceEvidenceProvider(str(database)).collect_trace(_context(store))
    assert trace.collection_complete is True
    assert trace.observations[0].trace_id == "bb" * 16
    assert trace.observations[0].session_id == "session-candidate"
    assert trace.observations[0].actual_model == "model-a"
    assert trace.observations[0].input_tokens == 42
    assert trace.observations[0].fallback_used is False
    assert trace.observations[0].finished is True

    logs = await SQLiteLogEvidenceProvider(str(database)).collect_logs(_context(store))
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
        _context(store)
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
        store.ingest_otlp_logs(
            _log_payload("run-1", "checkout:case", variant, trace_id)
        )
        window = _window("run-1", "checkout:case", variant, trace_id)
        if variant == "candidate":
            window = ExecutionWindow(**{**window.__dict__, "finished": None})
        _record_and_seal(store, window)

    trace = await SQLiteTraceEvidenceProvider(str(database)).collect_trace(_context(store))
    assert trace.observations[0].finished is None


async def test_trace_gate_ignores_non_api_request_metric_gaps(tmp_path: Path) -> None:
    database = tmp_path / "observability.sqlite3"
    store = LocalObservabilityStore(database)
    for variant, trace_id in (
        ("control", "aa" * 16),
        ("candidate", "bb" * 16),
    ):
        store.ingest_otlp_traces(
            _trace_payload("run-1", "checkout:case", variant, trace_id)
        )
        logs = _log_payload("run-1", "checkout:case", variant, trace_id)
        records = logs["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        records[:] = [records[0]]
        store.ingest_otlp_logs(logs)
        _record_and_seal(
            store, _window("run-1", "checkout:case", variant, trace_id)
        )

    context = _context(store)
    evidence = await SQLiteTraceEvidenceProvider(str(database)).collect_trace(context)
    result = evaluate_trace_gate(
        TraceGateSpec(expected_model="model-a"),
        evidence,
        run_id=context.run_id,
        cycle=context.cycle,
        candidate_ref=context.candidate_ref,
        candidate_digest=context.candidate_digest,
        policy_digest=context.policy_digest,
        expected_skill_digests=context.skill_digests,
        scenario_ids=set(context.scenario_ids),
        scenario_input_digests={"checkout:case": INPUT_DIGEST},
    )
    assert result.status is GateStatus.PASS


@pytest.mark.parametrize(
    ("missing_field", "missing_metric"),
    [
        ("model", "actual_model"),
        ("input_tokens", "input_tokens"),
        ("cache_read_tokens", "input_tokens"),
        ("cache_creation_tokens", "input_tokens"),
        ("fallback_used", "fallback_used"),
    ],
)
async def test_each_api_request_must_supply_trace_gate_metrics(
    tmp_path: Path, missing_field: str, missing_metric: str
) -> None:
    database = tmp_path / "observability.sqlite3"
    store = LocalObservabilityStore(database)
    for variant, trace_id in (
        ("control", "aa" * 16),
        ("candidate", "bb" * 16),
    ):
        store.ingest_otlp_traces(
            _trace_payload("run-1", "checkout:case", variant, trace_id)
        )
        logs = _log_payload("run-1", "checkout:case", variant, trace_id)
        if variant == "candidate":
            records = logs["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
            incomplete_request = {
                **records[0],
                "timeUnixNano": "450",
                "observedTimeUnixNano": "451",
                "attributes": [
                    item
                    for item in records[0]["attributes"]
                    if item["key"] != missing_field
                ],
            }
            records.insert(1, incomplete_request)
        store.ingest_otlp_logs(logs)
        _record_and_seal(
            store, _window("run-1", "checkout:case", variant, trace_id)
        )

    context = _context(store)
    evidence = await SQLiteTraceEvidenceProvider(str(database)).collect_trace(context)
    assert evidence.collector_error is None
    assert evidence.collection_complete is True
    assert getattr(evidence.observations[0], missing_metric) is None

    result = evaluate_trace_gate(
        TraceGateSpec(expected_model="model-a"),
        evidence,
        run_id=context.run_id,
        cycle=context.cycle,
        candidate_ref=context.candidate_ref,
        candidate_digest=context.candidate_digest,
        policy_digest=context.policy_digest,
        expected_skill_digests=context.skill_digests,
        scenario_ids=set(context.scenario_ids),
        scenario_input_digests={"checkout:case": INPUT_DIGEST},
    )
    assert result.status is GateStatus.BLOCKED
    assert missing_metric in result.summary


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


def test_ccb_debug_import_waits_for_complete_trailing_line(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    path = tmp_path / "session-partial.txt"
    line = "2026-08-24T10:00:00.000Z [ERROR] PartialError request 42 failed"
    path.write_text(line, encoding="utf-8")
    importer = CCBDebugLogImporter(store)

    assert importer.import_file(path) == 0
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert importer.import_file(path) == 1
    assert importer.import_file(path) == 0

    rows = store.search_logs(session_id="session-partial", level="ERROR")
    assert len(rows) == 1
    assert rows[0]["body"] == "PartialError request 42 failed"


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


def _diagnosis_log_payload(
    *,
    body: str,
    timestamp_ns: int,
    request_id: str | None = None,
    environment: str | None = "prod",
    deployment_version: str | None = "v1",
):
    attributes = [_attr("logger.name", "order.chain")]
    if request_id is not None:
        attributes.append(_attr("request.id", request_id))
    resource_attributes = [_attr("service.name", "order-api")]
    if environment is not None:
        resource_attributes.append(
            _attr("deployment.environment.name", environment)
        )
    if deployment_version is not None:
        resource_attributes.append(_attr("deployment.version", deployment_version))
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": resource_attributes},
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": str(timestamp_ns),
                                "observedTimeUnixNano": str(timestamp_ns),
                                "severityText": "ERROR",
                                "body": {"stringValue": body},
                                "attributes": attributes,
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_exact_identity_fallback_has_boundaries_and_nearest_first(
    tmp_path: Path,
) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    anchor = 1_000_000
    store.ingest_otlp_logs(
        _diagnosis_log_payload(body="request_id=req-10", timestamp_ns=anchor - 1)
    )
    store.ingest_otlp_logs(
        _diagnosis_log_payload(body="request_id=req-1", timestamp_ns=anchor + 100)
    )
    store.ingest_otlp_logs(
        _diagnosis_log_payload(
            body="structured req-1", timestamp_ns=anchor + 5, request_id="req-1"
        )
    )

    rows = store.search_logs_exact(
        identity_field="request_id",
        identity_value="req-1",
        service_name="order-api",
        start_time_ns=anchor - 200,
        end_time_ns=anchor + 200,
        anchor_time_ns=anchor,
        environment="prod",
        deployment_version="v1",
        logger="order.chain",
    )

    assert [row["timestamp_ns"] for row in rows] == [anchor + 5, anchor + 100]
    assert all("req-10" not in row["body"] for row in rows)


def test_exact_identity_json_fallback_requires_a_known_field(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    trace_id = "ab" * 16
    accepted = _diagnosis_log_payload(body="accepted", timestamp_ns=100)
    accepted["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0][
        "attributes"
    ].append(_attr("traceId", trace_id))
    rejected = _diagnosis_log_payload(body="rejected", timestamp_ns=101)
    rejected["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0][
        "attributes"
    ].append(_attr("diagnostic.note", trace_id))
    store.ingest_otlp_logs(accepted)
    store.ingest_otlp_logs(rejected)

    rows = store.search_logs_exact(
        identity_field="trace_id",
        identity_value=trace_id,
        service_name="order-api",
        start_time_ns=0,
        end_time_ns=200,
        anchor_time_ns=100,
        environment="prod",
        deployment_version="v1",
        logger="order.chain",
    )

    assert [row["body"] for row in rows] == ["accepted"]


def test_diagnosis_search_treats_missing_scope_as_exact_unknown(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    store.ingest_otlp_logs(
        _diagnosis_log_payload(
            body="dependency timeout req-null",
            timestamp_ns=100,
            request_id="req-null",
            environment=None,
            deployment_version=None,
        )
    )
    store.ingest_otlp_logs(
        _diagnosis_log_payload(
            body="dependency timeout req-null",
            timestamp_ns=101,
            request_id="req-null",
            environment="prod",
            deployment_version="v1",
        )
    )

    exact = store.search_logs_exact(
        identity_field="request_id",
        identity_value="req-null",
        service_name="order-api",
        start_time_ns=0,
        end_time_ns=200,
        anchor_time_ns=100,
        environment=None,
        deployment_version=None,
        logger="order.chain",
    )
    fuzzy = store.search_logs_bm25(
        terms=("dependency", "timeout"),
        service_name="order-api",
        start_time_ns=0,
        end_time_ns=200,
        environment=None,
        deployment_version=None,
        logger="order.chain",
    )

    assert [row["timestamp_ns"] for row in exact] == [100]
    assert [row["timestamp_ns"] for row in fuzzy] == [100]


def test_v5_log_schema_migrates_structured_index_fields_once(tmp_path: Path) -> None:
    database = tmp_path / "legacy-observability.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta(version INTEGER NOT NULL);
            INSERT INTO schema_meta(version) VALUES (5);
            CREATE TABLE log_records (
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
            """
        )
        connection.execute(
            """
            INSERT INTO log_records(
                observation_id, timestamp_ns, observed_time_ns, severity_number,
                severity_text, body, service_name, service_version,
                attributes_json, resource_json, raw_json, source, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-1",
                100,
                100,
                17,
                "ERROR",
                "legacy request req-legacy failed",
                "order-api",
                "v5",
                json.dumps(
                    {"request.id": "req-legacy", "logger.name": "legacy.logger"}
                ),
                json.dumps(
                    {
                        "service.name": "order-api",
                        "deployment.environment.name": "prod",
                        "deployment.version": "deploy-5",
                    }
                ),
                "{}",
                "otlp",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    store = LocalObservabilityStore(database)
    LocalObservabilityStore(database)  # migration is restart-idempotent
    rows = store.search_logs(request_id="req-legacy")

    assert len(rows) == 1
    assert rows[0]["environment"] == "prod"
    assert rows[0]["deployment_version"] == "deploy-5"
    assert rows[0]["logger"] == "legacy.logger"
    assert len(rows[0]["template_id"]) == 64


def test_concurrent_otlp_ingest_sequences_are_unique(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")

    def ingest(index: int) -> int:
        return store.ingest_otlp_logs(
            _diagnosis_log_payload(
                body=f"concurrent log {index}", timestamp_ns=10_000 + index
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        inserted = list(executor.map(ingest, range(16)))

    rows = store.search_logs(service_name="order-api", limit=1000)
    sequences = [row["ingest_sequence"] for row in rows]
    assert inserted == [1] * 16
    assert len(sequences) == 16
    assert len(set(sequences)) == 16


def test_invalid_otlp_trace_identifier_is_rejected(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    payload = _trace_payload("run-1", "checkout:case", "candidate", "not-a-trace")
    with pytest.raises(Exception, match="traceId/spanId"):
        store.ingest_otlp_traces(payload)


def _ingest_window_pair(
    store: LocalObservabilityStore, *, seal_candidate: bool = True
) -> tuple[ExecutionWindow, ExecutionWindow]:
    windows = []
    for variant, trace_id in (("control", "aa" * 16), ("candidate", "bb" * 16)):
        store.ingest_otlp_traces(
            _trace_payload("run-1", "checkout:case", variant, trace_id)
        )
        store.ingest_otlp_logs(
            _log_payload("run-1", "checkout:case", variant, trace_id)
        )
        window = _window("run-1", "checkout:case", variant, trace_id)
        store.record_execution(window)
        if variant == "control" or seal_candidate:
            store.record_otlp_flush_barrier(
                OtlpFlushBarrier(
                    flush_id=f"flush-{variant}",
                    collection_id=window.collection_id,
                    run_id=window.run_id,
                    cycle=window.cycle,
                    scenario_id=window.scenario_id,
                    variant=window.variant,
                    input_digest=window.input_digest,
                    signals=("traces", "logs"),
                    flush_started_at_ns=window.ended_at_ns,
                    flush_completed_at_ns=window.ended_at_ns,
                    deadline_ns=time.time_ns() + 10_000_000_000,
                )
            )
        windows.append(window)
    return windows[0], windows[1]


async def test_missing_otlp_flush_barrier_is_fail_closed(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    _ingest_window_pair(store, seal_candidate=False)

    evidence = await SQLiteTraceEvidenceProvider(str(store.path)).collect_trace(
        _context(store)
    )

    assert evidence.collection_complete is False
    assert "缺少 OTLP flush/watermark barrier" in (evidence.collector_error or "")

    with pytest.raises(Exception, match="等待 OTLP flush barrier 超时"):
        store.wait_for_otlp_flush_barrier(
            "collection-candidate", timeout_ms=1, poll_interval_ms=1
        )


async def test_timed_out_or_duplicate_otlp_barrier_is_fail_closed(
    tmp_path: Path,
) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    _, candidate = _ingest_window_pair(store, seal_candidate=False)
    base = {
        "collection_id": candidate.collection_id,
        "run_id": candidate.run_id,
        "cycle": candidate.cycle,
        "scenario_id": candidate.scenario_id,
        "variant": candidate.variant,
        "input_digest": candidate.input_digest,
        "signals": ("traces", "logs"),
        "flush_started_at_ns": candidate.ended_at_ns,
        "flush_completed_at_ns": candidate.ended_at_ns,
        "deadline_ns": candidate.ended_at_ns - 1,
    }
    store.record_otlp_flush_barrier(OtlpFlushBarrier(flush_id="late-1", **base))

    evidence = await SQLiteLogEvidenceProvider(str(store.path)).collect_logs(_context(store))
    assert evidence.collection_complete is False
    assert "超时" in (evidence.collector_error or "")

    duplicate = {**base, "deadline_ns": time.time_ns() + 10_000_000_000}
    store.record_otlp_flush_barrier(
        OtlpFlushBarrier(flush_id="late-2", **duplicate)
    )
    evidence = await SQLiteLogEvidenceProvider(str(store.path)).collect_logs(_context(store))
    assert evidence.collection_complete is False
    assert "不唯一" in (evidence.collector_error or "")


async def test_late_otlp_after_watermark_is_rejected_and_fail_closed(
    tmp_path: Path,
) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    _ingest_window_pair(store)
    late = _log_payload("run-1", "checkout:case", "candidate", "bb" * 16)
    record = late["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    record["timeUnixNano"] = "700"
    record["body"] = {"stringValue": "late exporter record"}

    assert store.ingest_otlp_logs(late) == 0
    evidence = await SQLiteBehaviorEvidenceProvider(str(store.path)).collect_behavior(
        _context(store)
    )

    assert evidence.collection_complete is False
    assert "晚到数据" in (evidence.collector_error or "")


async def test_barrier_can_arrive_before_execution_window_is_persisted(
    tmp_path: Path,
) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    for variant, trace_id in (("control", "aa" * 16), ("candidate", "bb" * 16)):
        store.ingest_otlp_traces(
            _trace_payload("run-1", "checkout:case", variant, trace_id)
        )
        store.ingest_otlp_logs(
            _log_payload("run-1", "checkout:case", variant, trace_id)
        )
        window = _window("run-1", "checkout:case", variant, trace_id)
        store.record_otlp_flush_barrier(
            OtlpFlushBarrier(
                flush_id=f"flush-before-window-{variant}",
                collection_id=window.collection_id,
                run_id=window.run_id,
                cycle=window.cycle,
                scenario_id=window.scenario_id,
                variant=window.variant,
                input_digest=window.input_digest,
                signals=("traces", "logs"),
                flush_started_at_ns=window.ended_at_ns,
                flush_completed_at_ns=window.ended_at_ns,
                deadline_ns=time.time_ns() + 10_000_000_000,
            )
        )
        store.record_execution(window)

    evidence = await SQLiteTraceEvidenceProvider(str(store.path)).collect_trace(
        _context(store)
    )
    assert evidence.collection_complete is True


async def test_receipt_manifest_rejects_replaced_sqlite_window(tmp_path: Path) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    _ingest_window_pair(store)
    context = _context(store)
    with store._connect() as connection:
        connection.execute(
            "UPDATE execution_windows SET collection_id = ? "
            "WHERE run_id = ? AND cycle = ? AND variant = ?",
            ("replacement-window", "run-1", 1, "candidate"),
        )

    evidence = await SQLiteLogEvidenceProvider(str(store.path)).collect_logs(context)
    assert evidence.collection_complete is False
    assert "replay receipt manifest" in (evidence.collector_error or "")


async def test_receipt_manifest_rejects_changed_window_and_barrier_digests(
    tmp_path: Path,
) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    _ingest_window_pair(store)
    context = _context(store)
    with store._connect() as connection:
        connection.execute(
            "UPDATE execution_windows SET result_sha256 = ? WHERE variant = ?",
            ("9" * 64, "candidate"),
        )
        connection.execute(
            "UPDATE otlp_flush_barriers SET flush_id = ? WHERE variant = ?",
            ("changed-flush", "control"),
        )

    evidence = await SQLiteBehaviorEvidenceProvider(str(store.path)).collect_behavior(
        context
    )
    assert evidence.collection_complete is False
    assert any(
        marker in (evidence.collector_error or "")
        for marker in ("result_sha256", "barrier digest")
    )
