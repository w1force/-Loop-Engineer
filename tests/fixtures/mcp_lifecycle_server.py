"""Stdio MCP server for request lifecycle tests.

It records real JSON-RPC cancellation notifications so tests can assert the
client sent `notifications/cancelled` with the exact request id.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time


TOOLS = [
    {
        "name": "sleep_echo",
        "description": "Return a value after a requested delay",
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "delay": {"type": "number"},
                "progress": {"type": "boolean"},
            },
            "required": ["value"],
        },
    }
]


def _send(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _record(payload: dict) -> None:
    path = os.environ.get("MCP_LIFECYCLE_LOG")
    if not path:
        return
    with _log_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _finish_call(request_id: int, value: str, delay: float) -> None:
    time.sleep(delay)
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": value}],
                "isError": False,
            },
        }
    )


def _exit_process(delay: float) -> None:
    time.sleep(delay)
    os._exit(0)


def _handle(request: dict) -> None:
    method = request.get("method")
    if method == "notifications/cancelled":
        _record({"type": "cancelled", "params": request.get("params") or {}})
        return
    if "id" not in request:
        return
    request_id = int(request["id"])
    if method == "initialize":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "lifecycle", "version": "0.1.0"},
                },
            }
        )
        return
    if method == "tools/list":
        _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        params = request.get("params") or {}
        args = params.get("arguments") or {}
        meta = params.get("_meta") or {}
        value = str(args.get("value", ""))
        delay = float(args.get("delay", 0))
        if value == "__exit__":
            _record({"type": "call", "id": request_id, "value": value, "delay": delay})
            sys.exit(0)
        if value == "__exit_after__":
            _record({"type": "call", "id": request_id, "value": value, "delay": delay})
            threading.Thread(target=_exit_process, args=(delay,), daemon=True).start()
            return
        if args.get("progress"):
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": meta.get("progressToken"),
                        "progress": 1,
                        "total": 2,
                        "message": f"working:{value}",
                    },
                }
            )
        _record({"type": "call", "id": request_id, "value": value, "delay": delay})
        threading.Thread(
            target=_finish_call,
            args=(request_id, value, delay),
            daemon=True,
        ).start()
        return
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        }
    )


_stdout_lock = threading.Lock()
_log_lock = threading.Lock()


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        _handle(json.loads(line))


if __name__ == "__main__":
    main()
