"""Assemble the automation loop: registry + discovery scan + outbox worker + scheduler.

    daemon:   AutomationService.build(config).run_forever()
    one-shot: await service.scan_once("incremental") / service.drain_once()

Agent invocation is behind the injected RunHandler; with the default LoggingRunHandler
the whole loop runs safely (scan + enqueue + drain) without a coordinator wired.
"""

from __future__ import annotations

import asyncio
import logging

from core.discovery.pipeline import DiscoveryPipeline
from core.state.store import LoopStateStore

from .discovery_job import DiscoveryJob, DiscoveryRunSummary
from .registry import ApplicationRegistry, AutomationConfig
from .scheduler import IntervalScheduler
from .worker import LoggingRunHandler, OutboxWorker, RunHandler, WorkerRunSummary

logger = logging.getLogger("automation.service")


class AutomationService:
    def __init__(
        self,
        *,
        config: AutomationConfig,
        state: LoopStateStore,
        discovery_job: DiscoveryJob,
        worker: OutboxWorker,
    ):
        self.config = config
        self.state = state
        self.discovery_job = discovery_job
        self.worker = worker

    @classmethod
    def build(
        cls, config: AutomationConfig, *, run_handler: RunHandler | None = None
    ) -> "AutomationService":
        state = LoopStateStore(config.state_db)
        registry = ApplicationRegistry(config.sources)
        pipeline = DiscoveryPipeline(state)
        discovery_job = DiscoveryJob(registry=registry, state=state, pipeline=pipeline)
        worker = OutboxWorker(
            registry=registry, state=state, run_handler=run_handler or LoggingRunHandler()
        )
        return cls(config=config, state=state, discovery_job=discovery_job, worker=worker)

    def build_scheduler(self) -> IntervalScheduler:
        scheduler = IntervalScheduler()
        scheduler.add_interval(
            "incremental-scan",
            lambda: self.discovery_job.run_once(mode="incremental"),
            interval_seconds=self.config.incremental_interval_seconds,
            jitter_seconds=self.config.incremental_jitter_seconds,
        )
        scheduler.add_daily(
            "full-sweep",
            lambda: self.discovery_job.run_once(mode="full"),
            at=self.config.daily_full_at,
        )
        scheduler.add_interval(
            "drain-outbox",
            lambda: self.worker.drain_once(),
            interval_seconds=self.config.worker_interval_seconds,
        )
        return scheduler

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        ready = len(ApplicationRegistry(self.config.sources).ready_sources())
        logger.info(
            "automation starting: %d/%d sources ready, incremental=%ds daily=%s worker=%ds",
            ready, len(self.config.sources),
            self.config.incremental_interval_seconds, self.config.daily_full_at,
            self.config.worker_interval_seconds,
        )
        await self.build_scheduler().run_forever(stop)

    async def scan_once(self, mode: str = "incremental") -> DiscoveryRunSummary:
        return await self.discovery_job.run_once(mode=mode)

    async def drain_once(self) -> WorkerRunSummary:
        return await self.worker.drain_once()


__all__ = ["AutomationService"]
