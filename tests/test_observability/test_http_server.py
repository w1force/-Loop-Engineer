from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import threading

from core.observability import LocalObservabilityStore, normalized_input_digest
from core.observability.server import ObservabilityHTTPServer


TOKEN = "c" * 32


def _request(
    port: int,
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    token: str | None = None,
) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None if payload is None else json.dumps(payload)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    result = response.status, json.loads(response.read())
    connection.close()
    return result


def test_otlp_is_loopback_ingestable_but_execution_windows_require_coordinator(
    tmp_path: Path,
) -> None:
    store = LocalObservabilityStore(tmp_path / "observability.sqlite3")
    server = ObservabilityHTTPServer(
        ("127.0.0.1", 0),
        store,
        coordinator_token=TOKEN,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    input_payload = {"prompt": "reproduce"}
    execution = {
        "run_id": "run-1",
        "cycle": 1,
        "scenario_id": "checkout:case",
        "variant": "candidate",
        "input_digest": normalized_input_digest(input_payload),
        "input_payload": input_payload,
        "collection_id": "candidate-window",
        "control_ref": "control",
        "control_digest": "a" * 64,
        "candidate_ref": "candidate",
        "candidate_digest": "b" * 64,
        "policy_digest": "c" * 64,
        "skill_digests": {"checkout": "d" * 64},
        "started_at_ns": 1,
        "ended_at_ns": 2,
        "collection_complete": True,
        "finished": True,
        "outcome": "success",
        "payload": {"status": 200},
        "model": "model-a",
        "tool_calls": [],
    }
    try:
        assert _request(port, "GET", "/healthz")[0] == 200
        assert _request(port, "POST", "/v1/logs", {"resourceLogs": []})[0] == 200
        assert _request(port, "POST", "/api/v1/executions", execution)[0] == 401

        bad = {**execution, "collection_complete": "false"}
        assert _request(
            port,
            "POST",
            "/api/v1/executions",
            bad,
            token=TOKEN,
        )[0] == 400
        assert _request(
            port,
            "POST",
            "/api/v1/executions",
            execution,
            token=TOKEN,
        )[0] == 201
        assert _request(port, "GET", "/api/v1/logs")[0] == 401
        assert _request(
            port,
            "GET",
            "/api/v1/logs?run_id=run-1&variant=candidate&start_time_ns=0",
            token=TOKEN,
        )[0] == 200
        assert _request(
            port,
            "GET",
            "/api/v1/logs?variant=production",
            token=TOKEN,
        )[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
