"""Application registry — the trusted per-service config the automation layer reads.

Maps a service to its run-log source, repository control ref/workspace, reviewers
and suppression window. The bridge (discovery -> DiagnosisRequest) and the worker
take control_ref/control_workspace from HERE, never from the (untrusted) signal.

Addresses (log paths, repo/workspace) may be left blank and filled in later: a
source with no ``log_path`` is simply skipped by the scanner until configured, so
the scheduler runs safely as a no-op before any service is wired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceConfig:
    service: str
    source_id: str
    log_path: str | None = None          # ← fill in later; None/"" => skipped
    control_ref: str | None = None       # ← repo ref of the frozen baseline
    control_workspace: str | None = None  # ← read-only control checkout
    candidate_workspace_root: str | None = None
    reviewers: tuple[str, ...] = ()
    suppression_seconds: int = 3600
    full_rescan: bool = False            # daily job resets the cursor for this source

    @property
    def ready(self) -> bool:
        """True once the addresses needed to actually scan + diagnose are set."""

        return bool(self.log_path and self.control_ref and self.control_workspace)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceConfig":
        if "service" not in data or "source_id" not in data:
            raise ValueError("source config requires 'service' and 'source_id'")
        return cls(
            service=str(data["service"]),
            source_id=str(data["source_id"]),
            log_path=data.get("log_path") or None,
            control_ref=data.get("control_ref") or None,
            control_workspace=data.get("control_workspace") or None,
            candidate_workspace_root=data.get("candidate_workspace_root") or None,
            reviewers=tuple(data.get("reviewers", ()) or ()),
            suppression_seconds=int(data.get("suppression_seconds", 3600)),
            full_rescan=bool(data.get("full_rescan", False)),
        )


@dataclass(frozen=True)
class AutomationConfig:
    """Top-level automation config. All cadences have safe defaults."""

    sources: tuple[SourceConfig, ...] = ()
    state_db: str = ".loop-engineer/state.db"
    incremental_interval_seconds: int = 300  # PRD FR-AUTO-002: every 5 minutes
    daily_full_at: str = "03:17"             # local HH:MM for the daily full sweep
    worker_interval_seconds: int = 60         # how often the worker drains the queue
    incremental_jitter_seconds: int = 20

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutomationConfig":
        return cls(
            sources=tuple(
                SourceConfig.from_dict(item) for item in data.get("sources", [])
            ),
            state_db=str(data.get("state_db", ".loop-engineer/state.db")),
            incremental_interval_seconds=int(
                data.get("incremental_interval_seconds", 300)
            ),
            daily_full_at=str(data.get("daily_full_at", "03:17")),
            worker_interval_seconds=int(data.get("worker_interval_seconds", 60)),
            incremental_jitter_seconds=int(data.get("incremental_jitter_seconds", 20)),
        )

    @classmethod
    def from_json_file(cls, path: str | Path) -> "AutomationConfig":
        return cls.from_dict(json.loads(Path(path).expanduser().read_text("utf-8")))


class ApplicationRegistry:
    """Lookup of SourceConfig by service, with a 'ready to scan' filter."""

    def __init__(self, sources: tuple[SourceConfig, ...] = ()):
        self._by_service: dict[str, SourceConfig] = {}
        for source in sources:
            self._by_service[source.service] = source

    @property
    def sources(self) -> tuple[SourceConfig, ...]:
        return tuple(self._by_service.values())

    def ready_sources(self) -> tuple[SourceConfig, ...]:
        """Sources whose addresses are configured; the rest are skipped safely."""

        return tuple(s for s in self._by_service.values() if s.ready)

    def get(self, service: str) -> SourceConfig | None:
        return self._by_service.get(service)


__all__ = [
    "ApplicationRegistry",
    "AutomationConfig",
    "SourceConfig",
]
