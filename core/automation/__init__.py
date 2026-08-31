"""Automation & scheduling layer for Loop Engineer.

Turns the one-shot ``DiscoveryPipeline.scan`` into a running system: a dependency-free
asyncio scheduler drives a 5-minute incremental scan + a daily full sweep, each
enqueuing eligible incidents into the durable outbox (cron never calls an agent), and
an OutboxWorker drains that queue into the LoopEngineer via a pluggable RunHandler.
"""

from __future__ import annotations

from .discovery_job import (
    ACTION_DIAGNOSE_ONLY,
    ACTION_LOOP_RUN,
    DiscoveryJob,
    DiscoveryRunSummary,
)
from .registry import ApplicationRegistry, AutomationConfig, SourceConfig
from .scheduler import IntervalScheduler, seconds_until_daily
from .service import AutomationService
from .worker import LoggingRunHandler, OutboxWorker, RunHandler, WorkerRunSummary

__all__ = [
    "ACTION_DIAGNOSE_ONLY",
    "ACTION_LOOP_RUN",
    "ApplicationRegistry",
    "AutomationConfig",
    "AutomationService",
    "DiscoveryJob",
    "DiscoveryRunSummary",
    "IntervalScheduler",
    "LoggingRunHandler",
    "OutboxWorker",
    "RunHandler",
    "SourceConfig",
    "WorkerRunSummary",
    "seconds_until_daily",
]
