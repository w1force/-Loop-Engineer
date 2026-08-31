"""DiscoveryPipeline end-to-end: JSONL logs -> detect -> dedup -> incident -> DiagnosisRequest."""

from __future__ import annotations

import json
from pathlib import Path

from core.connectors.logs import JsonlRunLogConnector
from core.discovery import (
    DiscoveryPipeline,
    incident_to_diagnosis_request,
)
from core.state import LoopStateStore


def _line(kind: str, **payload) -> str:
    return json.dumps(
        {"ts": "2026-01-01T00:00:00+00:00", "seq": 1, "chain_id": "c1", "kind": kind, "payload": payload}
    )


def _setup(tmp_path: Path):
    st = LoopStateStore(tmp_path / "state.db")
    conn = JsonlRunLogConnector(st, source_id="ccb-run")
    pipe = DiscoveryPipeline(st)
    return st, conn, pipe


def test_scan_detects_and_dedups_incidents(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    st, conn, pipe = _setup(tmp_path)
    log.write_text(
        "\n".join(
            [
                _line("turn_start"),  # not an error -> ignored
                _line("run_error", error_type="TimeoutError", message="mcp call timed out after 30000ms"),
                _line("run_error", error_type="TimeoutError", message="mcp call timed out after 45000ms"),  # same class -> dedup
                _line("tool_input_malformed", tool="Read"),
            ]
        )
        + "\n",
        "utf-8",
    )

    result = pipe.scan(conn, log, service="ccb", suppression_seconds=3600)

    assert result.records_scanned == 4
    # two distinct incident classes: the timeout (deduped to 1) + the malformed-input
    assert len(result.new_incidents) == 2
    rules = {i.matched_rule for i in result.new_incidents}
    assert "mcp.timeout.no_fallback" in rules
    assert "mcp.tool_input_malformed" in rules
    # the second timeout line deduped into the existing incident (not a 3rd incident)
    assert len(result.updated_incidents) == 1
    timeout = next(i for i in result.new_incidents if i.matched_rule == "mcp.timeout.no_fallback")
    assert st.get_incident(timeout.incident_id)["occurrences"] == 2

    # a second scan of the same file finds nothing new (cursor advanced)
    assert pipe.scan(conn, log, service="ccb").records_scanned == 0


def test_eligibility_gates_auto_fix(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    st, conn, pipe = _setup(tmp_path)
    log.write_text(_line("provider_error", error_type="AuthError", message="401") + "\n", "utf-8")
    result = pipe.scan(conn, log, service="ccb")
    assert len(result.new_incidents) == 1
    # provider errors that are not timeouts are diagnose-only, not auto-fix
    assert result.new_incidents[0].eligibility == "diagnose_only"


def test_bridge_builds_valid_diagnosis_request(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    st, conn, pipe = _setup(tmp_path)
    log.write_text(
        _line("run_error", error_type="TimeoutError", message="timed out", original_input={"prompt": "checkout"})
        + "\n",
        "utf-8",
    )
    result = pipe.scan(conn, log, service="ccb")
    incident = result.new_incidents[0]

    req = incident_to_diagnosis_request(
        incident, control_workspace=str(tmp_path), control_ref="rev-abc"
    )
    assert req.incident_id == incident.incident_id
    assert req.matched_rule == "mcp.timeout.no_fallback"
    assert req.control_ref == "rev-abc"
    assert len(req.error_logs) == 1
    assert req.error_logs[0].uri.startswith("signal://ccb-run@")
    assert req.error_logs[0].uri.endswith("#0")
    assert len(req.error_logs[0].sha256) == 64


def test_empty_original_input_is_preserved_as_a_frozen_value(tmp_path: Path) -> None:
    log = tmp_path / "run.jsonl"
    _state, connector, pipeline = _setup(tmp_path)
    log.write_text(
        _line(
            "run_error",
            error_type="TimeoutError",
            message="timed out",
            original_input={},
        )
        + "\n",
        "utf-8",
    )
    incident = pipeline.scan(connector, log, service="ccb").new_incidents[0]

    request = incident_to_diagnosis_request(
        incident, control_workspace=str(tmp_path), control_ref="rev-abc"
    )

    assert request.primary_signal.original_input == {}


def test_same_trace_different_error_logs_form_one_incident_context(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    _state, connector, pipeline = _setup(tmp_path)
    log.write_text(
        "\n".join(
            [
                _line(
                    "run_error",
                    error_type="TimeoutError",
                    message="dependency timed out",
                    trace_id="trace-shared",
                ),
                _line(
                    "tool_input_malformed",
                    tool="Read",
                    message="bad downstream input",
                    trace_id="trace-shared",
                ),
            ]
        )
        + "\n",
        "utf-8",
    )

    result = pipeline.scan(connector, log, service="ccb")
    assert len(result.new_incidents) == 1
    incident = result.new_incidents[0]
    assert incident.occurrences == 2
    request = incident_to_diagnosis_request(
        incident,
        control_workspace=str(tmp_path),
        control_ref="rev-abc",
    )
    assert len(request.error_logs) == 2
    assert len(request.related_signals) == 1
    assert request.related_signals[0].correlation.trace_id == "trace-shared"


def test_rotated_file_with_identical_record_gets_a_new_signal_identity(
    tmp_path: Path,
) -> None:
    log = tmp_path / "run.jsonl"
    state, connector, pipeline = _setup(tmp_path)
    content = _line(
        "run_error", error_type="TimeoutError", message="dependency timed out"
    ) + "\n"
    log.write_text(content, "utf-8")
    first = pipeline.scan(connector, log, service="ccb")
    assert first.signals_recorded == 1

    log.rename(tmp_path / "run.jsonl.1")  # keep old inode allocated
    log.write_text(content, "utf-8")
    second = pipeline.scan(connector, log, service="ccb")

    assert second.records_scanned == 1
    assert second.signals_recorded == 1
    incident_id = first.new_incidents[0].incident_id
    assert state.get_incident(incident_id)["occurrences"] == 2


def test_bridge_bounds_related_signal_context_to_two_hundred(tmp_path: Path) -> None:
    sample = {
        "signal_id": "primary",
        "source_id": "logs",
        "kind": "run_error",
        "observed_at": "2026-01-01T00:00:00Z",
        "service": "orders",
        "message": "primary failure",
        "evidence": {"line_offset": 0, "source_generation": "1:1"},
    }
    related = tuple(
        {
            "signal_id": f"secondary-{index}",
            "correlation_reason": "trace_id",
            "payload": {
                **sample,
                "signal_id": f"secondary-{index}",
                "message": f"secondary failure {index}",
                "evidence": {
                    "line_offset": index + 1,
                    "source_generation": "1:1",
                },
            },
        }
        for index in range(250)
    )

    request = incident_to_diagnosis_request(
        {
            "incident_id": "incident-many",
            "matched_rule": "service.error",
            "sample": sample,
            "signals": related,
            "service": "orders",
        },
        control_workspace=str(tmp_path),
        control_ref="rev-control",
    )

    assert len(request.related_signals) == 200
    assert len(request.error_logs) == 201
