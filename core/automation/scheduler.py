"""A small dependency-free asyncio scheduler for the automation loop.

Deliberately not APScheduler/cron-daemon: the project is pure asyncio and we do not
want an external scheduler dependency. Provides interval jobs (e.g. the 5-minute
incremental scan) and a daily job (the full sweep), with the properties the PRD
requires:

- non-overlap: each job runs to completion before its next tick is scheduled, so a
  slow scan never overlaps itself (PRD FR-AUTO-002 "5 分钟扫描不得重叠");
- error isolation: a job raising is logged and the loop keeps going;
- jitter: interval jobs spread out to avoid thundering-herd alignment;
- graceful stop: an asyncio.Event stops every job promptly.

For external cron/systemd instead of a long-running daemon, call the jobs directly
(see ``core.automation.__main__ --once``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import random

logger = logging.getLogger("automation.scheduler")

JobFn = Callable[[], Awaitable[None]]


def seconds_until_daily(now: datetime, at_hh_mm: str) -> float:
    """Seconds from ``now`` to the next local occurrence of ``HH:MM``."""

    hour, minute = (int(part) for part in at_hh_mm.split(":", 1))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"invalid daily time: {at_hh_mm}")
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


@dataclass
class _Job:
    name: str
    fn: JobFn
    kind: str  # "interval" | "daily"
    interval_seconds: float = 0.0
    jitter_seconds: float = 0.0
    daily_at: str = "03:17"
    running: bool = field(default=False)


class IntervalScheduler:
    def __init__(self, *, now: Callable[[], datetime] = datetime.now):
        self._jobs: list[_Job] = []
        self._now = now

    def add_interval(
        self, name: str, fn: JobFn, *, interval_seconds: float, jitter_seconds: float = 0.0
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._jobs.append(
            _Job(
                name=name,
                fn=fn,
                kind="interval",
                interval_seconds=interval_seconds,
                jitter_seconds=max(0.0, jitter_seconds),
            )
        )

    def add_daily(self, name: str, fn: JobFn, *, at: str) -> None:
        seconds_until_daily(self._now(), at)  # validate format now
        self._jobs.append(_Job(name=name, fn=fn, kind="daily", daily_at=at))

    async def _run_guarded(self, job: _Job) -> None:
        """Run one job tick with non-overlap + error isolation."""

        if job.running:
            logger.warning("skip %s: previous run still in progress (no overlap)", job.name)
            return
        job.running = True
        try:
            await job.fn()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — isolate: one job failing must not kill the loop
            logger.exception("job %s failed", job.name)
        finally:
            job.running = False

    async def _sleep_or_stop(self, seconds: float, stop: asyncio.Event) -> bool:
        """Sleep up to ``seconds``; return True if stop fired (caller should exit)."""

        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    async def _interval_loop(self, job: _Job, stop: asyncio.Event) -> None:
        while not stop.is_set():
            delay = job.interval_seconds + (
                random.uniform(0, job.jitter_seconds) if job.jitter_seconds else 0.0
            )
            if await self._sleep_or_stop(delay, stop):
                return
            await self._run_guarded(job)

    async def _daily_loop(self, job: _Job, stop: asyncio.Event) -> None:
        while not stop.is_set():
            delay = seconds_until_daily(self._now(), job.daily_at)
            if await self._sleep_or_stop(delay, stop):
                return
            await self._run_guarded(job)

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Run all jobs until ``stop`` is set (or the task is cancelled)."""

        stop = stop or asyncio.Event()
        if not self._jobs:
            logger.warning("scheduler has no jobs registered")
        tasks = [
            asyncio.create_task(
                self._interval_loop(job, stop)
                if job.kind == "interval"
                else self._daily_loop(job, stop),
                name=f"job:{job.name}",
            )
            for job in self._jobs
        ]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["IntervalScheduler", "seconds_until_daily"]
