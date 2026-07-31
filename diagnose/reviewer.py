"""独立诊断审查 Agent 的配置、提示和只读权限。"""
from __future__ import annotations

from dataclasses import replace

from core.agent_loop import AgentConfig
from core.tools import CanUseDecision
from core.types import TextBlock, ToolUseBlock, UserMessage
from diagnose.model import DiagnosisReviewPolicy
from diagnose.review_tools import diagnosis_review_tools
from diagnose.session import DiagnosisSession

_READ_ONLY_CORE_TOOLS = {"Read", "Glob", "Grep", "LSP"}
_REVIEW_CONTROL_TOOLS = {
    "GetDiagnosisReviewContext",
    "ReadDiagnosisReviewEvidence",
    "SubmitDiagnosisReview",
}


def configure_diagnosis_review_agent(
    config: AgentConfig,
    session: DiagnosisSession,
    policy: DiagnosisReviewPolicy,
) -> AgentConfig:
    """返回隔离的 reviewer 配置；工具执行仍由 ToolContext 中的 actor 二次校验。"""
    caller_policy = config.can_use_tool

    async def can_review_use_tool(block: ToolUseBlock) -> CanUseDecision:
        name = block.name
        allowed_by_role = (
            name in _READ_ONLY_CORE_TOOLS
            or name in _REVIEW_CONTROL_TOOLS
            or (policy.allow_reviewer_mcp and name.startswith("mcp__"))
        )
        if not allowed_by_role:
            return CanUseDecision(allow=False, reason="review Agent is restricted to read-only diagnosis tools")
        caller_decision = await caller_policy(block)
        return caller_decision

    reminder = UserMessage(content=[TextBlock(text="""<system-reminder>
# Independent Diagnosis Review

Assume the proposed diagnosis may be wrong. Start with GetDiagnosisReviewContext and inspect every claim proposal and terminal hypothesis against its referenced evidence. Check for omitted contradictory evidence, confusion of symptoms with causes, and conclusions whose scope or duration exceeds the evidence. Use read-only MCP tools when a decisive captured interpretation cannot be independently checked. Submit exactly one structured SubmitDiagnosisReview call covering every proposal before ending. You cannot mutate diagnosis evidence, hypotheses, or proposals.

Review finding target rules:
- target_type=claim_proposal requires target_id equal to a claim proposal id.
- target_type=hypothesis requires target_id equal to a hypothesis id such as seed-*.
- target_type=evidence requires target_id equal to an EVD-* id.
- target_type=diagnosis requires target_id=null.

For MAT/MCP calls, verify the selected object's runtime type first. Use map-content tools only for Map objects; use ArrayList/list inspection, dominator children, inbound references, or GC-root paths for lists. Do not retry a type-incompatible tool blindly.
</system-reminder>""")])
    return replace(
        config,
        tools=[*config.tools, *diagnosis_review_tools()],
        initial_messages=[*config.initial_messages, reminder],
        can_use_tool=can_review_use_tool,
    )
