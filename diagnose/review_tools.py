"""独立审查 Agent 的只读上下文与结构化提交工具。"""
from __future__ import annotations

import json

from pydantic import BaseModel, field_validator

from core.tools import Tool, ToolContext, build_tool
from diagnose.model import DiagnosisReview
from diagnose.tool_context import require_diagnosis_state


class EmptyReviewInput(BaseModel):
    pass


class ReadReviewEvidenceInput(BaseModel):
    evidence_ids: list[str] | None = None


class SubmitDiagnosisReviewInput(BaseModel):
    review: DiagnosisReview

    @field_validator("review", mode="before")
    @classmethod
    def coerce_review_json(cls, value):
        return json.loads(value) if isinstance(value, str) else value


def diagnosis_review_tools() -> list[Tool]:
    async def get_context(_: EmptyReviewInput, tc: ToolContext) -> str:
        state, session = require_diagnosis_state(tc, actor="reviewer")
        return _json(session.get_review_context())

    async def read_evidence(inp: ReadReviewEvidenceInput, tc: ToolContext) -> str:
        state, session = require_diagnosis_state(tc, actor="reviewer")
        records = session.catalog.all()
        if inp.evidence_ids is not None:
            wanted = set(inp.evidence_ids)
            records = [record for record in records if record.id in wanted]
        return _json([record.model_dump(mode="json") for record in records])

    async def submit_review(inp: SubmitDiagnosisReviewInput, tc: ToolContext) -> str:
        state, session = require_diagnosis_state(tc, actor="reviewer")
        if state.diagnose_review_round is None or state.diagnose_review_revision is None:
            raise RuntimeError("review round and revision are not bound to AgentState")
        if inp.review.reviewed_revision != state.diagnose_review_revision:
            raise RuntimeError("model review revision does not match trusted AgentState revision")
        result = session.submit_review(inp.review, round_index=state.diagnose_review_round)
        return _json({"status": "accepted", "review": result.model_dump(mode="json")})

    return [
        build_tool(name="GetDiagnosisReviewContext", description="读取当前 revision 的只读诊断审查快照。", input_model=EmptyReviewInput, func=get_context, is_concurrency_safe=True),
        build_tool(name="ReadDiagnosisReviewEvidence", description="按 EVD-* 读取待审查证据。", input_model=ReadReviewEvidenceInput, func=read_evidence, is_concurrency_safe=True),
        build_tool(name="SubmitDiagnosisReview", description="提交覆盖全部 claim proposal 的结构化独立审查。", input_model=SubmitDiagnosisReviewInput, func=submit_review),
    ]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
