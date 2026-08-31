"""Incremental JSONL run-log connector (the discovery-side "log store" reader).

Reads a service's structured run-log (the FileTracer format:
``{"ts","seq","chain_id","turn","depth","kind","payload"}`` — one JSON object per
line) incrementally, persisting an inode+byte-offset cursor in the
:class:`~core.state.store.LoopStateStore` so restarts never re-emit old lines
(PRD FR-AUTO-002). Rotation (inode change) or truncation resets the offset.

The line parser is pluggable via ``record_adapter`` so CCB's real format — once it
is refactored to be runnable — maps onto the same :class:`LogRecord` without
touching detection/discovery. The existing ``CCBDebugLogImporter`` handles CCB's
current ``--debug`` *text* logs into the verification-evidence store; this connector
is the structured discovery feed that carries actionable Agent/MCP error semantics
(run_error / provider_error / tool errors with error_type, model, tokens).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
from typing import Any, Callable

from core.state.store import LoopStateStore


@dataclass(frozen=True)
class LogRecord:
    source_id: str
    line_offset: int
    ts: str | None
    seq: int | None
    chain_id: str | None
    turn: int | None
    kind: str
    payload: dict[str, Any]
    raw: dict[str, Any]
    # Stable for one physical file generation.  It prevents a rotated file whose
    # first record has the same byte offset/content shape from reusing an old
    # signal id.  The default preserves compatibility with custom adapters.
    source_generation: str = ""


def default_run_log_adapter(
    obj: dict[str, Any], *, source_id: str, line_offset: int
) -> LogRecord | None:
    """Map one FileTracer JSONL object to a LogRecord. Returns None to skip a line."""

    kind = obj.get("kind")
    if not isinstance(kind, str):
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return LogRecord(
        source_id=source_id,
        line_offset=line_offset,
        ts=obj.get("ts"),
        seq=obj.get("seq") if isinstance(obj.get("seq"), int) else None,
        chain_id=obj.get("chain_id"),
        turn=obj.get("turn") if isinstance(obj.get("turn"), int) else None,
        kind=kind,
        payload=payload,
        raw=obj,
    )


RecordAdapter = Callable[..., "LogRecord | None"]


class JsonlRunLogConnector:
    """Incrementally reads new lines from one JSONL run-log file."""

    def __init__(
        self,
        state_store: LoopStateStore,
        *,
        source_id: str,
        record_adapter: RecordAdapter = default_run_log_adapter,
    ):
        self.state = state_store
        self.source_id = source_id
        self.adapter = record_adapter

    def read_new(self, path: str | Path) -> list[LogRecord]:
        """Return log records appended since the last saved cursor and advance it."""

        source = Path(path).expanduser().resolve()
        if not source.is_file():
            return []
        records: list[LogRecord] = []
        with source.open("rb") as handle:
            # Use the descriptor we actually read, rather than a path stat that can
            # race with rename-based rotation between stat() and open().
            st = os.fstat(handle.fileno())
            generation = f"{st.st_dev}:{st.st_ino}"
            cursor = self.state.get_cursor(self.source_id)
            offset = 0
            if (
                cursor is not None
                and cursor["path"] == str(source)
                and cursor["inode"] == st.st_ino
                and st.st_size >= cursor["byte_offset"]
            ):
                offset = cursor["byte_offset"]
            # else: new file / rotation (inode change) / truncation -> restart at 0
            new_offset = offset
            handle.seek(offset)
            for raw_line in handle:
                if not raw_line.endswith(b"\n"):
                    break  # partial trailing line: leave the cursor before it
                line_offset = new_offset
                new_offset = handle.tell()
                try:
                    text = raw_line.decode("utf-8").rstrip("\r\n")
                except UnicodeDecodeError:
                    continue
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                record = self.adapter(
                    obj, source_id=self.source_id, line_offset=line_offset
                )
                if record is not None:
                    records.append(replace(record, source_generation=generation))

        self.state.set_cursor(
            self.source_id, path=str(source), inode=st.st_ino, byte_offset=new_offset
        )
        return records


__all__ = [
    "JsonlRunLogConnector",
    "LogRecord",
    "RecordAdapter",
    "default_run_log_adapter",
]
