"""Discovery job: scan configured sources -> enqueue eligible incidents.

This is the cron-facing half. It NEVER invokes an agent (PRD §16 "Cron 只负责入队，
不直接调用 Agent"); it scans, dedups (via the pipeline + suppression window) and
records an idempotent run-intent in the outbox. A separate worker
(:mod:`core.automation.worker`) drains that queue and drives the LoopEngineer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Callable

from core.connectors.logs import JsonlRunLogConnector
from core.discovery.contracts import Eligibility
from core.discovery.pipeline import DiscoveryPipeline
from core.state.store import LoopStateStore, content_digest

from .registry import ApplicationRegistry, SourceConfig

logger = logging.getLogger("automation.discovery")

# Outbox action types the worker understands.
ACTION_LOOP_RUN = "loop-run"          # auto-fix eligible: diagnose -> repair -> verify -> PR
ACTION_DIAGNOSE_ONLY = "diagnose-only"  # mid-risk: produce diagnosis, no auto repair

ConnectorFactory = Callable[[str], JsonlRunLogConnector]


@dataclass(frozen=True)
class DiscoveryRunSummary:
    mode: str
    sources_scanned: int
    sources_skipped: int
    records_scanned: int
    incidents_new: int
    enqueued_auto_fix: int
    enqueued_diagnose_only: int


class DiscoveryJob:
    def __init__(
        self,
        *,
        registry: ApplicationRegistry,
        state: LoopStateStore,
        pipeline: DiscoveryPipeline,
        connector_factory: ConnectorFactory | None = None,
    ):
        self.registry = registry
        self.state = state
        self.pipeline = pipeline
        self._connector_factory = connector_factory or (
            lambda source_id: JsonlRunLogConnector(state, source_id=source_id)
        )

    async def run_once(self, *, mode: str = "incremental") -> DiscoveryRunSummary:
        ready = self.registry.ready_sources()
        skipped = len(self.registry.sources) - len(ready)
        if not ready:
            logger.info("discovery: no configured sources yet (mode=%s) — skipping", mode)
            return DiscoveryRunSummary(mode, 0, skipped, 0, 0, 0, 0)

        records = new_incidents = auto_fix = diagnose_only = 0
        for source in ready:
            try:
                result = await self._scan_source(source, mode=mode)
            except Exception:  # noqa: BLE001 — one bad source must not abort the sweep
                logger.exception("discovery: source %s failed", source.service)
                continue
            records += result.records_scanned
            new_incidents += len(result.new_incidents)
            for incident in result.new_incidents:
                enqueued = self._enqueue(incident, source)
                if enqueued == ACTION_LOOP_RUN:
                    auto_fix += 1
                elif enqueued == ACTION_DIAGNOSE_ONLY:
                    diagnose_only += 1
        summary = DiscoveryRunSummary(
            mode, len(ready), skipped, records, new_incidents, auto_fix, diagnose_only
        )
        logger.info(
            "discovery(%s): sources=%d records=%d new=%d enqueued(auto_fix=%d diagnose=%d)",
            mode, summary.sources_scanned, records, new_incidents, auto_fix, diagnose_only,
        )
        return summary

    async def _scan_source(self, source: SourceConfig, *, mode: str):
        if mode == "full" and source.full_rescan:
            self.state.reset_cursor(source.source_id)
        connector = self._connector_factory(source.source_id)
        # pipeline.scan is synchronous (file IO + sqlite) — keep the loop responsive.
        return await asyncio.to_thread(
            self.pipeline.scan,
            connector,
            source.log_path,
            service=source.service,
            suppression_seconds=source.suppression_seconds,
        )

    def _enqueue(self, incident, source: SourceConfig) -> str | None:
        """Enqueue an idempotent run-intent based on eligibility. Returns action type."""

        eligibility = incident.eligibility
        if eligibility == Eligibility.AUTO_FIX_ELIGIBLE.value:
            action = ACTION_LOOP_RUN
        elif eligibility == Eligibility.DIAGNOSE_ONLY.value:
            action = ACTION_DIAGNOSE_ONLY
        else:  # RECORD_ONLY — recorded by discovery, nothing to enqueue
            return None
        key = f"{action}:{incident.incident_id}"
        # stable per incident (independent of occurrence count) so re-enqueue is a no-op
        request_digest = content_digest({"incident_id": incident.incident_id, "action": action})
        newly = self.state.enqueue_outbox(
            idempotency_key=key,
            action_type=action,
            request_digest=request_digest,
            payload={
                "incident_id": incident.incident_id,
                "service": source.service,
                "matched_rule": incident.matched_rule,
            },
        )
        if newly:
            logger.info("enqueued %s for incident %s", action, incident.incident_id)
        return action


__all__ = [
    "ACTION_DIAGNOSE_ONLY",
    "ACTION_LOOP_RUN",
    "DiscoveryJob",
    "DiscoveryRunSummary",
]
