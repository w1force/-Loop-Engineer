"""Repair-stage contracts.

The concrete ``RepairResult`` / ``RepairCycleRequest`` models currently live in
``core.verification.workflow`` for historical reasons and are heavily depended on by
the frozen-plan machinery. This module is the canonical repair-stage import surface
and re-exports them, plus adds the structured ``RepairFeedbackBundle`` that replaces
the old unstructured ``previous_failures: tuple[str, ...]``.

TODO(arch): physically relocate the ``RepairResult`` / ``RepairCycleRequest``
definitions here once the verification namespace split is complete; keep this facade
so existing importers do not break.
"""

from __future__ import annotations

from pydantic import Field

from core.verification.workflow import RepairCycleRequest, RepairResult

from .base import Contract
from .failure import FailureRecord


class RepairFeedbackBundle(Contract):
    """Owner-filtered feedback for the next repair round.

    Only ``owner == REPAIR`` findings are ever placed here; infrastructure, policy,
    integrity and observability failures never reach the repair agent.
    """

    cycle: int = Field(ge=1)
    findings: tuple[FailureRecord, ...] = ()


__all__ = ["RepairCycleRequest", "RepairFeedbackBundle", "RepairResult"]
