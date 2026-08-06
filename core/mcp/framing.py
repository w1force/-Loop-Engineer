"""MCP stdio JSON-RPC framing helpers.

Current demo/TDA servers use one JSON object per line. Some stdio tools use
`Content-Length` framing, so the reader accepts both without changing manager
or tool adapter code.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

MCPStdioFraming = Literal["newline", "content-length"]


def serialize_jsonrpc_message(
    payload: dict[str, Any],
    *,
    framing: MCPStdioFraming = "newline",
) -> bytes:
    """Serialize one JSON-RPC message for stdio transport."""

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if framing == "newline":
        return body + b"\n"
    if framing == "content-length":
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        return header + body
    raise ValueError(f"Unsupported MCP stdio framing: {framing}")


async def read_jsonrpc_message(
    reader: asyncio.StreamReader,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Read one JSON-RPC message from a stdio stream.

    The first line decides the framing:
    - `Content-Length: N` starts a header block followed by an exact body.
    - anything else is treated as a newline-delimited JSON message.
    """

    first = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=timeout)
    if first.lower().startswith(b"content-length:"):
        headers = [first]
        while True:
            line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=timeout)
            headers.append(line)
            if line in {b"\r\n", b"\n"}:
                break
        length = _parse_content_length(headers)
        body = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return _decode_json_object(body)
    return _decode_json_object(first)


def _parse_content_length(headers: list[bytes]) -> int:
    for header in headers:
        name, _, value = header.partition(b":")
        if name.lower() != b"content-length":
            continue
        try:
            length = int(value.strip())
        except ValueError as exc:
            raise ValueError("Invalid MCP Content-Length header") from exc
        if length < 0:
            raise ValueError("Invalid MCP Content-Length header")
        return length
    raise ValueError("Missing MCP Content-Length header")


def _decode_json_object(data: bytes) -> dict[str, Any]:
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("MCP JSON-RPC message must be an object")
    return value
