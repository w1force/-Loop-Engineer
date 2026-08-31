#!/usr/bin/env python3
"""Create a deterministic, body-free HAR safe enough for a second secret scan."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTED = "__REDACTED__"
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
}


def _redact_url(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    query = urlencode([(name, REDACTED) for name, _ in parse_qsl(parts.query, keep_blank_values=True)])
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _redact_headers(headers: Any) -> int:
    count = 0
    if not isinstance(headers, list):
        return count
    for header in headers:
        if isinstance(header, dict) and str(header.get("name", "")).lower() in SENSITIVE_HEADERS:
            header["value"] = REDACTED
            count += 1
    return count


def _redact_cookies(cookies: Any) -> int:
    count = 0
    if not isinstance(cookies, list):
        return count
    for cookie in cookies:
        if isinstance(cookie, dict) and "value" in cookie:
            cookie["value"] = REDACTED
            count += 1
    return count


def _redact_named_values(values: Any) -> int:
    count = 0
    if not isinstance(values, list):
        return count
    for item in values:
        if isinstance(item, dict) and "value" in item:
            item["value"] = REDACTED
            count += 1
    return count


def sanitize(document: dict[str, Any]) -> dict[str, int]:
    report = {
        "entries": 0,
        "headers": 0,
        "cookies": 0,
        "query_values": 0,
        "form_values": 0,
        "bodies_removed": 0,
    }
    entries = document.get("log", {}).get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("HAR log.entries must be a list")

    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each HAR entry must be an object")
        report["entries"] += 1
        for side in ("request", "response"):
            message = entry.get(side, {})
            if not isinstance(message, dict):
                raise ValueError(f"HAR entry {side} must be an object")
            report["headers"] += _redact_headers(message.get("headers"))
            report["cookies"] += _redact_cookies(message.get("cookies"))

        request = entry.get("request", {})
        if isinstance(request.get("url"), str):
            parsed = urlsplit(request["url"])
            report["query_values"] += len(parse_qsl(parsed.query, keep_blank_values=True))
            request["url"] = _redact_url(request["url"])
        query = request.get("queryString")
        report["query_values"] += _redact_named_values(query)

        post_data = request.get("postData")
        if isinstance(post_data, dict):
            if "text" in post_data:
                post_data.pop("text", None)
                report["bodies_removed"] += 1
            report["form_values"] += _redact_named_values(post_data.get("params"))

        response = entry.get("response", {})
        if isinstance(response.get("redirectURL"), str):
            response["redirectURL"] = _redact_url(response["redirectURL"])
        content = response.get("content")
        if isinstance(content, dict) and "text" in content:
            content.pop("text", None)
            content.pop("encoding", None)
            report["bodies_removed"] += 1

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    source = args.input.resolve()
    destination = args.output.resolve()
    if source == destination:
        parser.error("input and output must be different files")
    if destination.exists():
        parser.error("output already exists; refusing to overwrite it")

    document = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "log" not in document:
        parser.error("input is not a HAR object")
    report: dict[str, Any] = sanitize(document)
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    destination.write_bytes(payload)
    report["output_sha256"] = hashlib.sha256(payload).hexdigest()
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
