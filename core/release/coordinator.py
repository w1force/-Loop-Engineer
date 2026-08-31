"""Narrow adapter from a signed Coordinator outcome to ReleaseManager."""

from __future__ import annotations

import asyncio

from core.verification.coordinator import VerifiedReleaseRequest

from .github import ReleaseError, ReleaseManager
from .models import ReleaseRequest


class CoordinatorReleaseAction:
    """Expose no generic publish method to the orchestration layer."""

    def __init__(self, manager: ReleaseManager, request: ReleaseRequest):
        if type(manager) is not ReleaseManager:
            raise TypeError("Coordinator release requires the built-in ReleaseManager")
        self.manager = manager
        self.request = ReleaseRequest.model_validate_json(request.model_dump_json())

    async def release_verified(self, verified: VerifiedReleaseRequest) -> str:
        verified = VerifiedReleaseRequest.model_validate_json(
            verified.model_dump_json()
        )
        if (
            self.request.verification_run_id != verified.run_id
            or self.request.verification_cycle != verified.cycle
            or self.request.verification_incident_id != verified.incident_id
            or self.request.verification_incident_digest != verified.incident_digest
            or self.request.verification_plan_digest != verified.plan_digest
            or self.request.verification_replay_digest != verified.replay_digest
            or self.request.verification_replay_manifest != verified.replay_manifest
            or self.request.verification_scenario_input_digests
            != verified.scenario_input_digests
        ):
            raise ReleaseError(
                "ReleaseRequest 与 Coordinator VERIFIED Plan/Incident 绑定不一致"
            )
        receipt = await asyncio.to_thread(self.manager.release, self.request)
        return receipt.pull_request_url


__all__ = ["CoordinatorReleaseAction"]
