"""Neutral, immutable base for cross-stage contract artifacts.

Mirrors ``core.verification.models.VerificationModel`` (frozen, extra-forbid) but
lives in the provider/verification-agnostic ``core.contracts`` layer so stage and
orchestrator code can describe data shapes without importing the verification
namespace. Trusted digests and machine reports still originate in
``core.verification``; these contracts carry the *shapes* that flow between stages.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)
