"""Loopback-only OTLP/HTTP JSON receiver and evidence query API."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import threading
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .store import (
    CCBDebugLogImporter,
    ExecutionWindow,
    LocalObservabilityStore,
    ObservabilityStoreError,
    OtlpFlushBarrier,
)


MAX_REQUEST_BYTES = 16 * 1024 * 1024


class _Handler(BaseHTTPRequestHandler):
    server: "ObservabilityHTTPServer"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _authorized(self, token: str | None) -> bool:
        if not token:
            return True
        expected = f"Bearer {token}"
        return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("invalid Content-Length")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _respond(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            path = urlparse(self.path).path
            token = (
                self.server.otlp_token
                if path in {"/v1/traces", "/v1/logs"}
                else self.server.coordinator_token
            )
            if not self._authorized(token):
                self._respond(401, {"error": "unauthorized"})
                return
            payload = self._json_body()
            if path == "/v1/traces":
                count = self.server.store.ingest_otlp_traces(payload)
                self._respond(200, {"partialSuccess": {}, "inserted": count})
                return
            if path == "/v1/logs":
                count = self.server.store.ingest_otlp_logs(payload)
                self._respond(200, {"partialSuccess": {}, "inserted": count})
                return
            if path == "/api/v1/executions":
                self.server.store.record_execution(
                    ExecutionWindow(
                        run_id=str(payload["run_id"]),
                        cycle=_strict_int(payload["cycle"]),
                        scenario_id=str(payload["scenario_id"]),
                        variant=payload["variant"],
                        input_digest=str(payload["input_digest"]),
                        input_payload=payload["input_payload"],
                        collection_id=str(payload["collection_id"]),
                        control_ref=str(payload["control_ref"]),
                        control_digest=str(payload["control_digest"]),
                        candidate_ref=str(payload["candidate_ref"]),
                        candidate_digest=str(payload["candidate_digest"]),
                        policy_digest=str(payload["policy_digest"]),
                        skill_digests=dict(payload["skill_digests"]),
                        started_at_ns=_strict_int(payload["started_at_ns"]),
                        ended_at_ns=_strict_int(payload["ended_at_ns"]),
                        collection_complete=_strict_bool(
                            payload["collection_complete"]
                        ),
                        trace_id=_optional_string(payload.get("trace_id")),
                        request_id=_optional_string(payload.get("request_id")),
                        session_id=_optional_string(payload.get("session_id")),
                        finished=_optional_bool(payload.get("finished")),
                        outcome=payload.get("outcome"),
                        payload=payload.get("payload"),
                        model=_optional_string(payload.get("model")),
                        tool_calls=(
                            tuple(_tool_call(item) for item in payload["tool_calls"])
                            if payload.get("tool_calls") is not None
                            else None
                        ),
                    )
                )
                self._respond(201, {"recorded": True})
                return
            if path == "/api/v1/otlp-flush-barriers":
                signals = payload["signals"]
                if not isinstance(signals, list) or any(
                    not isinstance(item, str) for item in signals
                ):
                    raise ValueError("signals must be a string array")
                self.server.store.record_otlp_flush_barrier(
                    OtlpFlushBarrier(
                        flush_id=str(payload["flush_id"]),
                        collection_id=str(payload["collection_id"]),
                        run_id=str(payload["run_id"]),
                        cycle=_strict_int(payload["cycle"]),
                        scenario_id=str(payload["scenario_id"]),
                        variant=payload["variant"],
                        input_digest=str(payload["input_digest"]),
                        signals=tuple(signals),
                        flush_started_at_ns=_strict_int(
                            payload["flush_started_at_ns"]
                        ),
                        flush_completed_at_ns=_strict_int(
                            payload["flush_completed_at_ns"]
                        ),
                        deadline_ns=_strict_int(payload["deadline_ns"]),
                    )
                )
                self._respond(201, {"recorded": True})
                return
            self._respond(404, {"error": "not_found"})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._respond(400, {"error": f"invalid_request: {exc}"})
        except ObservabilityStoreError as exc:
            self._respond(400, {"error": f"invalid_evidence: {exc}"})
        except Exception as exc:
            self._respond(500, {"error": f"store_error: {type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/healthz":
                self._respond(200, {"status": "ok"})
                return
            if not self._authorized(self.server.coordinator_token):
                self._respond(401, {"error": "unauthorized"})
                return
            if parsed.path.startswith("/api/v1/traces/"):
                trace_id = unquote(parsed.path.removeprefix("/api/v1/traces/"))
                spans = self.server.store.trace_payload(trace_id)
                self._respond(200 if spans else 404, {"trace_id": trace_id, "spans": spans})
                return
            if parsed.path == "/api/v1/logs":
                query = parse_qs(parsed.query)
                logs = self.server.store.search_logs(
                    trace_id=_first(query, "trace_id"),
                    session_id=_first(query, "session_id"),
                    run_id=_first(query, "run_id"),
                    scenario_id=_first(query, "scenario_id"),
                    variant=_first(query, "variant"),
                    service_name=_first(query, "service_name"),
                    level=_first(query, "level"),
                    start_time_ns=_optional_query_int(query, "start_time_ns"),
                    end_time_ns=_optional_query_int(query, "end_time_ns"),
                    limit=int(_first(query, "limit") or "200"),
                )
                self._respond(200, {"logs": logs})
                return
            self._respond(404, {"error": "not_found"})
        except (ValueError, ObservabilityStoreError) as exc:
            self._respond(400, {"error": str(exc)})


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strict_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected JSON integer")
    return value


def _strict_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected JSON boolean")
    return value


def _optional_bool(value: Any) -> bool | None:
    return None if value is None else _strict_bool(value)


def _tool_call(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("tool_calls entries must be JSON objects")
    expected = {"sequence", "tool_name", "input_digest", "output_digest", "outcome"}
    if set(value) != expected:
        raise ValueError("tool_calls entry fields are invalid")
    return dict(value)


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _optional_query_int(query: dict[str, list[str]], key: str) -> int | None:
    value = _first(query, key)
    return None if value is None else int(value)


class ObservabilityHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: LocalObservabilityStore,
        *,
        coordinator_token: str,
        otlp_token: str | None = None,
    ):
        host = address[0]
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("本地 observability 服务只允许绑定 loopback")
        if len(coordinator_token) < 32:
            raise ValueError("coordinator token 至少需要 32 个字符")
        if otlp_token is not None and len(otlp_token) < 32:
            raise ValueError("OTLP token 至少需要 32 个字符")
        self.store = store
        self.coordinator_token = coordinator_token
        self.otlp_token = otlp_token
        super().__init__(address, _Handler)


def serve(
    *,
    database: str | Path,
    host: str = "127.0.0.1",
    port: int = 4318,
    coordinator_token: str,
    otlp_token: str | None = None,
    ccb_debug_dir: str | Path | None = None,
    import_interval_seconds: float = 2.0,
) -> None:
    store = LocalObservabilityStore(database)
    server = ObservabilityHTTPServer(
        (host, port),
        store,
        coordinator_token=coordinator_token,
        otlp_token=otlp_token,
    )
    stop = threading.Event()
    importer_thread: threading.Thread | None = None
    if ccb_debug_dir is not None:
        importer = CCBDebugLogImporter(store)

        def import_loop() -> None:
            while not stop.wait(import_interval_seconds):
                try:
                    importer.import_directory(ccb_debug_dir)
                except (OSError, ObservabilityStoreError):
                    pass

        importer_thread = threading.Thread(
            target=import_loop,
            name="ccb-debug-importer",
            daemon=True,
        )
        importer_thread.start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        server.server_close()
        if importer_thread is not None:
            importer_thread.join(timeout=max(import_interval_seconds * 2, 1))


__all__ = ["ObservabilityHTTPServer", "serve"]
