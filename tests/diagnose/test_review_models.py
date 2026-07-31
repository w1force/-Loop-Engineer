"""审查模型与 AgentState 绑定的最小回归覆盖。"""
import asyncio

import pytest
from pydantic import ValidationError

from core.tools import ToolContext
from core.types import AgentState
from diagnose.model import (
    DiagnosisReview,
    DiagnosisReviewPolicy,
    ProposalReview,
    ProposalReviewVerdict,
    ReviewDecision,
)
from telemetry.tracer import NoopTracer


def test_review_policy_has_bounded_rework_rounds():
    assert DiagnosisReviewPolicy().max_rework_rounds == 1
    with pytest.raises(ValidationError):
        DiagnosisReviewPolicy(max_rework_rounds=6)


def test_approved_review_cannot_contain_rejected_proposal():
    with pytest.raises(ValidationError):
        DiagnosisReview(
            reviewed_revision=1,
            decision=ReviewDecision.APPROVED,
            proposal_reviews=[ProposalReview(proposal_id="p", verdict=ProposalReviewVerdict.REJECT, rationale="no")],
        )


def test_agent_state_flat_diagnosis_bindings_reach_tool_context():
    state = AgentState(diagnose_actor="reviewer", diagnose_review_round=1, diagnose_review_revision=9)
    context = ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=state)
    assert context.agent_state is state
    assert context.agent_state is not None
    assert context.agent_state.diagnose_actor == "reviewer"
