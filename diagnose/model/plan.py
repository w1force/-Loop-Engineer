"""计划与动作调用模型

包含 AnalysisActionRequest、ActionInvocation 和 DiagnosisPlan。
DiagnosisPlan 由 Task 4 引入,描述一期诊断协调层产出的执行计划。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class AnalysisActionRequest(BaseModel):
    """分析动作请求

    表示对某个分析动作的执行请求，包含动作 ID 和参数。
    """

    action_id: str
    hypothesis_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class ActionInvocation(BaseModel):
    """动作调用记录

    记录一个动作的实际执行情况，包括状态、产生的证据等。
    """

    id: str
    action_id: str
    arguments: dict[str, Any]
    hypothesis_id: str | None
    status: Literal["completed", "cached", "rejected", "failed"]
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class DiagnosisPlan(BaseModel):
    """诊断计划

    由 DiagnosisPlanner 产出的执行计划。allowed_action_ids 与 rejected_actions
    互斥: 每个 action 要么进入 allowed (本期可执行),要么进入 rejected (附原因),
    保证计划决策完全可审计。budget 为剩余 action 调用次数预算。
    """

    allowed_action_ids: list[str] = Field(default_factory=list)
    rejected_actions: dict[str, str] = Field(default_factory=dict)
    budget: int = 0
    seed_hypothesis_ids: list[str] = Field(default_factory=list)
