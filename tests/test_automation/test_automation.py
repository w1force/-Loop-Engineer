"""Automation layer: scheduler mechanics + real scan -> enqueue -> drain flow."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path

import pytest

from core.automation import (
    ACTION_LOOP_RUN,
    ApplicationRegistry,
    AutomationConfig,
    AutomationService,
    SourceConfig,
)
from core.automation.scheduler import IntervalScheduler, seconds_until_daily, _Job


# ── scheduler mechanics ───────────────────────────────────────────────────────

def test_seconds_until_daily_wraps_to_next_day():
    assert seconds_until_daily(datetime(2026, 1, 1, 3, 0, 0), "03:17") == 1020.0
    # already past today -> next day
    assert seconds_until_daily(datetime(2026, 1, 1, 4, 0, 0), "03:17") == pytest.approx(
        23 * 3600 + 17 * 60, abs=1
    )


@pytest.mark.asyncio
async def test_run_guarded_is_non_overlapping():
    gate = asyncio.Event()
    runs = 0

    async def slow():
        nonlocal runs
        runs += 1
        await gate.wait()

    scheduler = IntervalScheduler()
    job = _Job(name="j", fn=slow, kind="interval", interval_seconds=1)
    first = asyncio.create_task(scheduler._run_guarded(job))
    await asyncio.sleep(0)  # let first acquire
    await scheduler._run_guarded(job)  # second tick while first in-flight -> skipped
    assert runs == 1
    gate.set()
    await first
    assert job.running is False


@pytest.mark.asyncio
async def test_run_guarded_isolates_errors():
    async def boom():
        raise RuntimeError("job blew up")

    scheduler = IntervalScheduler()
    job = _Job(name="boom", fn=boom, kind="interval", interval_seconds=1)
    await scheduler._run_guarded(job)  # must not raise
    assert job.running is False


# ── config / registry ─────────────────────────────────────────────────────────

def test_config_from_dict_and_ready_filter(tmp_path: Path):
    cfg = AutomationConfig.from_dict(
        {
            "state_db": str(tmp_path / "s.db"),
            "sources": [
                {"service": "unconfigured", "source_id": "u"},  # no addresses yet
                {
                    "service": "ccb",
                    "source_id": "ccb-log",
                    "log_path": str(tmp_path / "ccb.jsonl"),
                    "control_ref": "main",
                    "control_workspace": str(tmp_path / "control"),
                },
            ],
        }
    )
    reg = ApplicationRegistry(cfg.sources)
    assert [s.service for s in reg.ready_sources()] == ["ccb"]


@pytest.mark.asyncio
async def test_no_ready_sources_is_a_safe_noop(tmp_path: Path):
    svc = AutomationService.build(
        AutomationConfig(state_db=str(tmp_path / "s.db"))
    )
    summary = await svc.scan_once("incremental")
    assert summary.sources_scanned == 0 and summary.enqueued_auto_fix == 0


# ── real end-to-end: scan -> enqueue -> drain ─────────────────────────────────

class _RecordingHandler:
    def __init__(self):
        self.calls = []

    async def handle(self, *, action_type, incident, source, request):
        self.calls.append((action_type, incident["incident_id"], request))
        return f"run:{incident['incident_id']}"


def _write_line(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(obj) + "\n")


@pytest.mark.asyncio
async def test_scan_enqueue_drain_end_to_end(tmp_path: Path):
    log = tmp_path / "ccb.jsonl"
    config = AutomationConfig(
        state_db=str(tmp_path / "state.db"),
        sources=(
            SourceConfig(
                service="ccb",
                source_id="ccb-log",
                log_path=str(log),
                control_ref="base-sha",
                control_workspace=str(tmp_path / "control"),
            ),
        ),
    )
    handler = _RecordingHandler()
    svc = AutomationService.build(config, run_handler=handler)

    # a real MCP timeout run_error line -> mcp.timeout.no_fallback (AUTO_FIX_ELIGIBLE)
    _write_line(
        log,
        {
            "kind": "run_error",
            "payload": {"error_type": "TimeoutError", "message": "MCP tool timed out"},
        },
    )

    scan = await svc.scan_once("incremental")
    assert scan.records_scanned == 1
    assert scan.incidents_new == 1
    assert scan.enqueued_auto_fix == 1

    # cron enqueued but did NOT run an agent
    assert handler.calls == []

    drained = await svc.drain_once()
    assert drained.processed == 1 and drained.failed == 0
    assert len(handler.calls) == 1
    action, incident_id, request = handler.calls[0]
    assert action == ACTION_LOOP_RUN
    assert request.matched_rule == "mcp.timeout.no_fallback"
    assert request.control_ref == "base-sha"  # from trusted registry, not the signal

    # idempotent: the same fault again is deduped, nothing new enqueued
    _write_line(
        log,
        {
            "kind": "run_error",
            "payload": {"error_type": "TimeoutError", "message": "MCP tool timed out"},
        },
    )
    scan2 = await svc.scan_once("incremental")
    assert scan2.enqueued_auto_fix == 0
    drained2 = await svc.drain_once()
    assert drained2.processed == 0  # queue already drained


@pytest.mark.asyncio
async def test_worker_marks_failed_on_unknown_incident(tmp_path: Path):
    config = AutomationConfig(state_db=str(tmp_path / "state.db"))
    svc = AutomationService.build(config)
    svc.state.enqueue_outbox(
        idempotency_key="loop-run:ghost",
        action_type=ACTION_LOOP_RUN,
        request_digest="d" * 64,
        payload={"incident_id": "ghost", "service": "nope"},
    )
    result = await svc.drain_once()
    assert result.failed == 1 and result.processed == 0
    assert svc.state.get_outbox("loop-run:ghost")["status"] == "failed"
