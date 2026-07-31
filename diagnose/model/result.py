"""诊断结果模型

包含 DiagnosisStatus 枚举和 DiagnosisResult 模型。
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from diagnose.model.hypothesis import Claim, ClaimProposal, Hypothesis
from diagnose.model.evidence import EvidenceRecord
from diagnose.model.plan import ActionInvocation
from diagnose.model.review import ReviewCycle


class DiagnosisStatus(str, Enum):
    """诊断状态枚举"""

    COMPLETE = "complete"
    INCONCLUSIVE = "inconclusive"
    INSUFFICIENT_CAPABILITY = "insufficient_capability"
    INVALID_INPUT = "invalid_input"
    INCOMPLETE = "incomplete"


class DiagnosisResult(BaseModel):
    """诊断结果

    完整的诊断输出，包含根因、因果链、验证结论和所有支持数据。
    跨 catalog 引用与审查一致性由 DiagnosisSession 在构造结果前校验。
    """

    case_id: str
    platform_id: str
    status: DiagnosisStatus
    root_cause_category: str = "unknown"
    root_cause: str | None = None
    causal_chain: list[str] = Field(default_factory=list)
    validated_claims: list[Claim] = Field(default_factory=list)
    unvalidated_claims: list[Claim] = Field(default_factory=list)
    claim_proposals: list[ClaimProposal] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    invocations: list[ActionInvocation] = Field(default_factory=list)
    remediation_steps: list[str] = Field(default_factory=list)
    missing_capabilities: list[str] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)
    review_history: list[ReviewCycle] = Field(default_factory=list)
    review_complete: bool = False
    workflow_incomplete_reason: str | None = None
