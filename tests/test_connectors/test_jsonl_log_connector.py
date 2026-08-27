"""JsonlRunLogConnector: incremental read, cursor persistence, rotation, partial line."""

from __future__ import annotations

import json
from pathlib import Path

from core.connectors.logs import JsonlRunLogConnector
from core.state import LoopStateStore


def _line(kind: str, **payload) -> str:
    return json.dumps({"ts": "2026-01-01T00:00:00+00:00", "seq": 1, "kind": kind, "payload": payload})


def _connector(tmp_path: Path):
    st = LoopStateStore(tmp_path / "state.db")
    return JsonlRunLogConnector(st, source_id="ccb-run"), st


def test_reads_only_new_lines_across_calls(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    conn, _ = _connector(tmp_path)

    log.write_text(_line("run_error", error_type="TimeoutError") + "\n", "utf-8")
    first = conn.read_new(log)
    assert len(first) == 1 and first[0].kind == "run_error"

    # nothing new
    assert conn.read_new(log) == []

    # append one more -> only the new line comes back
    with log.open("a", encoding="utf-8") as fh:
        fh.write(_line("turn_start") + "\n")
    second = conn.read_new(log)
    assert len(second) == 1 and second[0].kind == "turn_start"


def test_partial_trailing_line_is_not_emitted_until_complete(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    conn, _ = _connector(tmp_path)
    # write a complete line + a partial (no newline) line
    log.write_text(_line("run_error") + "\n" + '{"kind":"turn_start"', "utf-8")
    got = conn.read_new(log)
    assert len(got) == 1 and got[0].kind == "run_error"
    # complete the partial line
    with log.open("a", encoding="utf-8") as fh:
        fh.write(',"payload":{}}\n')
    got2 = conn.read_new(log)
    assert len(got2) == 1 and got2[0].kind == "turn_start"


def test_cursor_survives_new_connector_instance(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    st = LoopStateStore(tmp_path / "state.db")
    log.write_text(_line("run_error") + "\n", "utf-8")
    JsonlRunLogConnector(st, source_id="ccb-run").read_new(log)
    # a fresh connector (e.g. after restart) sharing the state store re-reads nothing
    again = JsonlRunLogConnector(st, source_id="ccb-run").read_new(log)
    assert again == []


def test_rotation_resets_offset(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    conn, _ = _connector(tmp_path)
    log.write_text(_line("run_error") + "\n" + _line("turn_end") + "\n", "utf-8")
    assert len(conn.read_new(log)) == 2
    # simulate rotation: replace file (new inode) with fewer bytes
    log.unlink()
    log.write_text(_line("provider_error") + "\n", "utf-8")
    rotated = conn.read_new(log)
    assert len(rotated) == 1 and rotated[0].kind == "provider_error"


def test_malformed_lines_are_skipped(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    conn, _ = _connector(tmp_path)
    log.write_text(
        "not json\n" + _line("run_error") + "\n" + "[1,2,3]\n", "utf-8"
    )
    got = conn.read_new(log)
    assert len(got) == 1 and got[0].kind == "run_error"
