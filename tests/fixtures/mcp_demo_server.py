"""Tiny stdio MCP server used by tests.

It implements only the JSON-RPC methods this project needs for the basic MCP
framework: initialize, tools/list, and tools/call.
"""
from __future__ import annotations

import json
import os
import sys
import time


TOOLS = [
    {
        "name": "echo_text",
        "description": "Return the provided text",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "large_text",
        "description": "Return repeated text of the requested size",
        "inputSchema": {
            "type": "object",
            "properties": {"size": {"type": "integer"}},
            "required": ["size"],
        },
    },
    {
        "name": "structured_json",
        "description": "Return structured diagnostic JSON",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "dominant_text",
        "description": "Return a structured payload whose main value is text",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "progress_then_text",
        "description": "Emit progress before returning final text",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _sleep_from_env(name: str) -> None:
    raw = os.environ.get(name)
    if raw:
        time.sleep(float(raw))


def _handle(request: dict) -> None:
    if "id" not in request:
        return
    method = request.get("method")
    if method == "initialize":
        _sleep_from_env("MCP_DEMO_INIT_DELAY")
        _send(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "demo", "version": "0.1.0"},
                },
            }
        )
        return
    if method == "tools/list":
        _sleep_from_env("MCP_DEMO_LIST_DELAY")
        _send({"jsonrpc": "2.0", "id": request["id"], "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        params = request.get("params") or {}
        if params.get("name") == "echo_text":
            text = (params.get("arguments") or {}).get("text", "")
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
                    },
                }
            )
            return
        if params.get("name") == "large_text":
            size = int((params.get("arguments") or {}).get("size", 0))
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "content": [{"type": "text", "text": "L" * size}],
                        "isError": False,
                    },
                }
            )
            return
        if params.get("name") == "structured_json":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "content": [],
                        "structuredContent": {
                            "status": "failed",
                            "root_cause": "timeout",
                            "evidence": ["trace-1", "log-2"],
                        },
                        "isError": False,
                    },
                }
            )
            return
        if params.get("name") == "dominant_text":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "content": [],
                        "structuredContent": {"text": "root cause is timeout"},
                        "isError": False,
                    },
                }
            )
            return
        if params.get("name") == "progress_then_text":
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progress": 1,
                        "total": 2,
                        "message": "halfway",
                    },
                }
            )
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "content": [{"type": "text", "text": "done"}],
                        "isError": False,
                    },
                }
            )
            return
        else:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32601, "message": "Unknown tool"},
                }
            )
            return
    _send(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        }
    )


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        _handle(json.loads(line))


if __name__ == "__main__":
    main()
