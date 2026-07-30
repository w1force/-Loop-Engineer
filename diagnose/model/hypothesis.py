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


class Hypothesis(BaseModel):
    """诊断假设

    代表对根因的可验证猜测，包含陈述、支持/反对证据和下一步动作。
    """

    id: str
    category: str  # 由 platform taxonomy 定义
    statement: str
    status: HypothesisStatus = HypothesisStatus.PENDING
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    next_action_ids: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    """诊断结论

    代表经过验证的诊断结论，包含陈述、证据引用和验证说明。
    """

    id: str
    statement: str
    status: ClaimStatus
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    validation_note: str | None = None  # ClaimValidator 降级时填原因
