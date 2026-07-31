"""假设与结论模型

包含 HypothesisStatus、ClaimStatus、Hypothesis 和 Claim。
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class HypothesisStatus(str, Enum):
    """假设状态枚举"""

    PENDING = "pending"
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    CONFIRMED = "confirmed"
    INCONCLUSIVE = "inconclusive"


class ClaimStatus(str, Enum):
    """结论状态枚举"""

    VALIDATED = "validated"
    UNVALIDATED = "unvalidated"


class EvidenceTimeBasis(str, Enum):
    """结论所依赖证据的时间基础。"""

    POINT_IN_TIME = "point_in_time"
    MULTI_SNAPSHOT = "multi_snapshot"
    INTERVAL_PROFILE = "interval_profile"
    EVENT_SEQUENCE = "event_sequence"
    STATIC = "static"
    UNKNOWN = "unknown"


class Hypothesis(BaseModel):
    """诊断假设

    代表对根因的可验证猜测，包含陈述、支持/反对证据和下一步动作。
    """

    id: str
    category: str  # 由 platform taxonomy 定义
    statement: str
    status: HypothesisStatus = HypothesisStatus.PENDING
    supporting_evidence_ids: list[str] = Field(
        default_factory=list,
        description="直接支持该假设的 EVD-*；不得放入仅说明证据不足的记录。",
    )
    contradicting_evidence_ids: list[str] = Field(
        default_factory=list,
        description="直接反驳该假设的 EVD-*；finding.outcome=unknown 不属于反驳。",
    )
    inconclusive_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "说明当前证据范围、时间基础或能力不足以判断该假设的 EVD-*。"
            "仅用于 status=inconclusive，不表示支持或反驳。"
        ),
    )
    next_action_ids: list[str] = Field(default_factory=list)
    status_note: str | None = None


class ClaimProposal(BaseModel):
    """由诊断 Agent 提交、等待独立审查的正向事实提案。"""

    id: str
    category: str
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    time_basis: EvidenceTimeBasis = EvidenceTimeBasis.UNKNOWN
    confidence: float | None = Field(default=None, ge=0, le=1)


class Claim(BaseModel):
    """诊断结论

    代表经过验证的诊断结论，包含陈述、证据引用和验证说明。
    """

    id: str
    category: str
    statement: str
    status: ClaimStatus
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    time_basis: EvidenceTimeBasis = EvidenceTimeBasis.UNKNOWN
    confidence: float | None = Field(default=None, ge=0, le=1)
    validation_note: str | None = None  # 审查拒绝或禁用审查时填原因
