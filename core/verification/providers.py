"""Verification 外部证据接口。

数据库 DSN、日志目录、Docker 地址等部署参数不属于本轮；后续适配器只需实现
这些协议。未注入 provider 时对应硬门禁会明确 BLOCKED。
"""

from __future__ import annotations

import re
from typing import Protocol, Self

from pydantic import Field, StrictInt, model_validator

from .models import (
    BehaviorEvidence,
    LogEvidence,
    TraceEvidence,
    VerificationModel,
)


class EvidenceCollectionContext(VerificationModel):
    run_id: str = Field(min_length=1)
    cycle: StrictInt = Field(ge=1, le=3)
    control_ref: str = Field(min_length=1)
    control_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_ref: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_names: tuple[str, ...] = Field(min_length=1)
    skill_digests: dict[str, str] = Field(min_length=1)
    scenario_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _skill_contract_is_complete(self) -> Self:
        if len(self.skill_names) != len(set(self.skill_names)):
            raise ValueError("skill_names 不能重复")
        if set(self.skill_digests) != set(self.skill_names):
            raise ValueError("skill_digests 必须精确覆盖 skill_names")
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in self.skill_digests.values()
        ):
            raise ValueError("skill_digests 必须是 SHA-256")
        if len(self.scenario_ids) != len(set(self.scenario_ids)):
            raise ValueError("scenario_ids 不能重复")
        return self


class TraceEvidenceProvider(Protocol):
    async def collect_trace(
        self, context: EvidenceCollectionContext
    ) -> TraceEvidence: ...


class LogEvidenceProvider(Protocol):
    async def collect_logs(
        self, context: EvidenceCollectionContext
    ) -> LogEvidence: ...


class BehaviorEvidenceProvider(Protocol):
    async def collect_behavior(
        self, context: EvidenceCollectionContext
    ) -> BehaviorEvidence: ...


__all__ = [
    "BehaviorEvidenceProvider",
    "EvidenceCollectionContext",
    "LogEvidenceProvider",
    "TraceEvidenceProvider",
]
