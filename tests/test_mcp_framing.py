"""MCP stdio JSON-RPC framing."""
from __future__ import annotations

import asyncio

import pytest

from core.mcp.framing import read_jsonrpc_message, serialize_jsonrpc_message


def _reader_from_bytes(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_reads_newline_delimited_jsonrpc_message():
    reader = _reader_from_bytes(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')

    msg = await read_jsonrpc_message(reader, timeout=0.1)

    assert msg == {"jsonrpc": "2.0", "id": 1, "result": {}}


async def test_reads_content_length_jsonrpc_message():
    body = b'{"jsonrpc":"2.0","id":2,"result":{"ok":true}}'
    payload = b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    reader = _reader_from_bytes(payload)

    msg = await read_jsonrpc_message(reader, timeout=0.1)

    assert msg == {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}


def test_serialize_jsonrpc_message_defaults_to_newline():
    data = serialize_jsonrpc_message({"jsonrpc": "2.0", "id": 1})

    assert data == b'{"jsonrpc":"2.0","id":1}\n'


def test_serialize_jsonrpc_message_supports_content_length():
    data = serialize_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1}, framing="content-length"
    )

    assert data.startswith(b"Content-Length: ")
    assert b"\r\n\r\n" in data
    assert data.endswith(b'{"jsonrpc":"2.0","id":1}')


def test_serialize_jsonrpc_message_rejects_unknown_framing():
    with pytest.raises(ValueError, match="Unsupported MCP stdio framing"):
        serialize_jsonrpc_message({"jsonrpc": "2.0"}, framing="binary")


async def test_content_length_reports_invalid_header():
    reader = _reader_from_bytes(b"Content-Length: nope\r\n\r\n{}")

    with pytest.raises(ValueError, match="Invalid MCP Content-Length header"):
        await read_jsonrpc_message(reader, timeout=0.1)
