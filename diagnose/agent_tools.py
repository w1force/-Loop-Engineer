"""诊断 Agent 的稳定控制工具。

MCP/core 分析工具由 AgentConfig.resolve_tools 原样注入；本模块只暴露诊断流程
控制能力，不映射或代理任何具体 MCP 工具。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.tools import Tool, ToolContext, build_tool
from diagnose.model import ClaimProposal, EvidenceFinding, EvidenceLocation, Hypothesis
from diagnose.session import DiagnosisSession
from diagnose.tool_context import require_diagnosis_state


class CaptureDiagnosisEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_ids: list[str] = Field(
        min_length=1,
        description="该观察来自的当前 case artifact ID；至少提供一个。",
    )
    analyzer_id: str = Field(
        min_length=1,
        description="产生或阅读该信息的 MCP/core 工具名，或人工分析来源标识。"
    )
    summary: str = Field(
        min_length=1,
        description="忠实描述工具输出所直接支持的观察，不在此宣称根因已确认。",
    )
    finding: EvidenceFinding = Field(
        description=(
            "机器校验使用的直接观察。outcome 只能是 present、absent 或 unknown；"
            "confirmed/validated 属于结论状态，不得写入 evidence finding。"
        )
    )

    @field_validator("finding", mode="before")
    @classmethod
    def _coerce_finding_str(cls, v):
        # 同 _coerce_hypothesis_str: 兼容 provider (GLM 系列实测 glm-4.7 / glm-5.2)
        # 把嵌套 EvidenceFinding 序列化成 JSON 字符串而非 object。
        if isinstance(v, str):
            return json.loads(v)
        return v

    locations: list[EvidenceLocation] = Field(
        default_factory=list,
        description="证据在 artifact 或源码中的可追溯位置。",
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "支撑 finding/summary 的原始工具片段、数字和来源参数。"
            "不要在 data 中重复 finding，也不要把诊断结论写成工具事实。"
        ),
    )
    confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="可选的观察提取置信度，不代表 claim 已通过审查。",
    )


class UpdateDiagnosisHypothesisInput(BaseModel):
    hypothesis: Hypothesis

    @field_validator("hypothesis", mode="before")
    @classmethod
    def _coerce_hypothesis_str(cls, v):
        # 兼容部分 provider (GLM 系列实测 glm-4.7 / glm-5.2) 把嵌套 model 序列化成
        # JSON 字符串而非 object; 不做这层兼容, ProposeDiagnosisClaim/UpdateDiagnosisHypothesis
        # 会被 pydantic ValidationError 全部拦下, 结构化结论进不了 session。
        if isinstance(v, str):
            return json.loads(v)
        return v


class ProposeDiagnosisClaimInput(BaseModel):
    proposal: ClaimProposal

    @field_validator("proposal", mode="before")
    @classmethod
    def _coerce_proposal_str(cls, v):
        # 同 _coerce_hypothesis_str: 兼容 provider 把 Claim 序列化成 JSON 字符串。
        if isinstance(v, str):
            return json.loads(v)
        return v


class ReadDiagnosisEvidenceInput(BaseModel):
    evidence_ids: list[str] | None = None


class EmptyInput(BaseModel):
    pass


def _session(tc: ToolContext) -> DiagnosisSession:
    """从 ToolContext.agent_state.diagnose_session 取诊断 session。

    替代旧闭包: session 不再被工具闭包捕获, 而是由调用方在 build_agent_state 之后
    赋值到 agent_state.diagnose_session, 工具经 ToolContext 取用 (扁平状态, 不藏 dict)。
    """
    return require_diagnosis_state(tc, actor="diagnostician")[1]


def diagnosis_control_tools() -> list[Tool]:
    """构造稳定诊断控制工具集 (不再闭包 session; 从 ToolContext.agent_state 取)。"""

    async def get_context(_: EmptyInput, tc: ToolContext) -> str:
        return _json(_session(tc).get_context())

    async def capture(inp: CaptureDiagnosisEvidenceInput, tc: ToolContext) -> str:
        record = _session(tc).capture_evidence(
            artifact_ids=inp.artifact_ids,
            analyzer_id=inp.analyzer_id,
            summary=inp.summary,
            finding=inp.finding,
            locations=inp.locations,
            data=inp.data,
            confidence=inp.confidence,
        )
        return _json(record.model_dump(mode="json"))

    async def update(inp: UpdateDiagnosisHypothesisInput, tc: ToolContext) -> str:
        return _json(_session(tc).update_hypothesis(inp.hypothesis).model_dump(mode="json"))

    async def read_evidence(inp: ReadDiagnosisEvidenceInput, tc: ToolContext) -> str:
        session = _session(tc)
        records = session.catalog.all()
        if inp.evidence_ids is not None:
            wanted = set(inp.evidence_ids)
            records = [record for record in records if record.id in wanted]
        return _json([record.model_dump(mode="json") for record in records])

    async def propose_claim(inp: ProposeDiagnosisClaimInput, tc: ToolContext) -> str:
        return _json(_session(tc).submit_claim_proposal(inp.proposal).model_dump(mode="json"))

    async def finalize(_: EmptyInput, tc: ToolContext) -> str:
        session = _session(tc)
        allowed, reasons = session.request_review()
        if not allowed:
            return _json({"status": "rejected", "reasons": reasons})
        return _json({"status": "review_requested", "revision": session.review_requested_revision})

    return [
        build_tool(
            name="GetDiagnosisContext",
            description="读取当前诊断 case、artifact、假设和已登记证据。",
            input_model=EmptyInput,
            func=get_context,
            is_concurrency_safe=True,
        ),
        build_tool(
            name="CaptureDiagnosisEvidence",
            description=(
                "把一项已分析的工具观察登记为 EVD-* 证据。每次调用必须关联当前 "
                "case artifact，并在顶层 finding 填写 kind、outcome、scope；可选 details "
                "保存结构化观察细节。"
                "outcome 只能是 present（直接观察到）、absent（明确未观察到）或 "
                "unknown（该证据无法判断）；禁止使用 confirmed、validated、likely、"
                "suspected。诊断确认属于 hypothesis/claim/review，不属于 evidence finding。"
                "data 仅保存 reviewer 可复核的原始输出片段、数字和调用参数。"
            ),
            input_model=CaptureDiagnosisEvidenceInput,
            func=capture,
        ),
        build_tool(
            name="ReadDiagnosisEvidence",
            description="读取已登记的 EVD-* 证据；可按 evidence_ids 过滤。",
            input_model=ReadDiagnosisEvidenceInput,
            func=read_evidence,
            is_concurrency_safe=True,
        ),
        build_tool(
            name="UpdateDiagnosisHypothesis",
            description=(
                "更新已有诊断假设的状态和证据方向。supporting_evidence_ids 仅放直接支持，"
                "contradicting_evidence_ids 仅放直接反驳，inconclusive_evidence_ids 放因范围、"
                "时间基础或能力限制而无法判断的证据；finding.outcome=unknown 不得当作反驳。"
            ),
            input_model=UpdateDiagnosisHypothesisInput,
            func=update,
        ),
        build_tool(
            name="ProposeDiagnosisClaim",
            description=(
                "提交等待独立审查的正向诊断事实；否定结论和无法确认的结论应更新 hypothesis。"
                "artifact_ids 必须覆盖所有 evidence_ids 对应证据的 artifact_ids 并集。"
                "Diagnostician 不能自行声明 validated。"
            ),
            input_model=ProposeDiagnosisClaimInput,
            func=propose_claim,
        ),
        build_tool(
            name="FinalizeDiagnosis",
            description="请求生成当前诊断结果；证据不足时返回 inconclusive，而不伪造根因。",
            input_model=EmptyInput,
            func=finalize,
        ),
    ]


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)
