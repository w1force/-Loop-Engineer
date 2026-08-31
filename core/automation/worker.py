"""Outbox worker: drain enqueued run-intents and drive the loop.

This is where agent invocation happens — deliberately OUT of the cron/scan path.
The worker leases a pending intent (marks it in_progress), bridges the incident into
a DiagnosisRequest using the trusted registry config, and hands it to a pluggable
:class:`RunHandler`. In production the handler wires ``LoopEngineer.run``; tests and
the ``--once`` CLI can inject a logging/fake handler so the queue mechanics work
without a fully-assembled coordinator.

Single-process lease only (mark in_progress). Multi-worker lease/heartbeat/fencing
and dead-letter are a documented P1 follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Protocol

from core.discovery.bridge import incident_to_diagnosis_request
from core.state.store import LoopStateStore

from .discovery_job import ACTION_DIAGNOSE_ONLY, ACTION_LOOP_RUN
from .registry import ApplicationRegistry, SourceConfig

logger = logging.getLogger("automation.worker")


class RunHandler(Protocol):
    async def handle(
        self, *, action_type: str, incident: dict[str, Any], source: SourceConfig, request: Any
    ) -> str:
        """Process one intent; return an external id (e.g. run_id). Raise on failure."""
        ...


class LoggingRunHandler:
    """Default handler: records intent, does not run an agent. For --once / tests."""

    async def handle(self, *, action_type, incident, source, request) -> str:
        logger.info(
            "would run %s for incident %s (service=%s) — no handler wired",
            action_type, incident.get("incident_id"), source.service,
        )
        return f"noop:{incident.get('incident_id')}"


@dataclass(frozen=True)
class WorkerRunSummary:
    processed: int
    failed: int
    skipped: int


class OutboxWorker:
    def __init__(
        self,
        *,
        registry: ApplicationRegistry,
        state: LoopStateStore,
        run_handler: RunHandler,
        action_types: tuple[str, ...] = (ACTION_LOOP_RUN, ACTION_DIAGNOSE_ONLY),
    ):
        self.registry = registry
        self.state = state
        self.run_handler = run_handler
        self.action_types = action_types

    async def drain_once(self) -> WorkerRunSummary:
        pending = self.state.list_outbox(status="pending")
        processed = failed = skipped = 0
        for row in pending:
            action_type = row["action_type"]
            if action_type not in self.action_types:
                skipped += 1
                continue
            key = row["idempotency_key"]
            payload = row["payload"]
            self.state.mark_outbox(key, status="in_progress")  # soft single-process lease
            try:
                external_id = await self._process(action_type, payload)
                self.state.mark_outbox(key, status="done", external_id=external_id)
                processed += 1
            except Exception as exc:  # noqa: BLE001 — isolate per-intent failures
                logger.exception("worker: intent %s failed", key)
                self.state.mark_outbox(key, status="failed", external_id=str(exc)[:200])
                failed += 1
        if processed or failed:
            logger.info("worker: processed=%d failed=%d skipped=%d", processed, failed, skipped)
        return WorkerRunSummary(processed, failed, skipped)

    async def _process(self, action_type: str, payload: dict[str, Any]) -> str:
        incident_id = payload["incident_id"]
        incident = self.state.get_incident(incident_id)
        if incident is None:
            raise LookupError(f"incident not found: {incident_id}")
        source = self.registry.get(payload["service"])
        if source is None or not source.ready:
            raise LookupError(f"no ready registry config for service {payload['service']}")
        request = incident_to_diagnosis_request(
            incident,
            control_workspace=source.control_workspace,
            control_ref=source.control_ref,
        )
        return await self.run_handler.handle(
            action_type=action_type, incident=incident, source=source, request=request
        )


__all__ = [
    "LoggingRunHandler",
    "OutboxWorker",
    "RunHandler",
    "WorkerRunSummary",
]
