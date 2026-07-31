"""DiagnosisWorkflow 不依赖真实模型的编排回归测试。"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from core.agent_loop import AgentConfig
from core.types import AgentState, Message, StreamEvent
from diagnose.api import create_diagnosis_session
from diagnose.model import (
    ArtifactKind,
    ArtifactRef,
    ClaimProposal,
    DiagnosisCase,
    DiagnosisReview,
    DiagnosisReviewPolicy,
    DiagnosisStatus,
    EvidenceFinding,
    FindingOutcome,
    HypothesisStatus,
    ProposalReview,
    ProposalReviewVerdict,
    ReviewDecision,
    ReviewMode,
    ReviewMode,
)
from diagnose.platform_impl.java_jvm.platform import JavaJvmDiagnosticPlatform
from diagnose.registry import PlatformRegistry
from diagnose.workflow import DiagnosisWorkflow
from telemetry.tracer import NoopTracer
from telemetry.tracer import Tracer


class _Provider:
    def stream(self, **_: object):
        async def events():
            if False:
                yield StreamEvent(type="message_stop")
        return events()

    def count_tokens(self, messages: list[Message]) -> int:
        return 0


class _ScriptedRunner:
    def __init__(self, script):
        self.script = script
        self.calls: list[str] = []

    def run(self, prompt: str, state: AgentState, config: AgentConfig, tracer: Tracer) -> AsyncIterator[dict]:
        async def events():
            self.calls.append(state.diagnose_actor or "unknown")
            action = self.script.pop(0)
            action(state)
            yield {"type": "result", "subtype": "success"}
        return events()


def _session(tmp_path):
    (tmp_path / "thread.txt").write_text("x")
    registry = PlatformRegistry()
    registry.register(JavaJvmDiagnosticPlatform())
    return create_diagnosis_session(DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path), artifacts=[ArtifactRef(id="thread", kind=ArtifactKind.THREAD_SNAPSHOT, path="thread.txt")]), registry)


def _prepare_diagnosis(state: AgentState) -> None:
    session = state.diagnose_session
    assert session is not None
    evidence = session.capture_evidence(
        artifact_ids=["thread"],
        analyzer_id="tda",
        summary="contention",
        finding=EvidenceFinding(
            kind="monitor_contention",
            outcome=FindingOutcome.PRESENT,
            scope="thread_snapshot",
        ),
    )
    for hypothesis in list(session.hypotheses):
        if hypothesis.id == "seed-lock-contention":
            session.update_hypothesis(hypothesis.model_copy(update={"status": HypothesisStatus.CONFIRMED, "supporting_evidence_ids": [evidence.id]}))
        else:
            session.update_hypothesis(hypothesis.model_copy(update={"status": HypothesisStatus.INCONCLUSIVE, "status_note": "not enough evidence"}))
    session.submit_claim_proposal(ClaimProposal(id="p", category="lock_contention", statement="workers contend", evidence_ids=[evidence.id], artifact_ids=["thread"]))
    assert session.request_review()[0]


def _approve(state: AgentState) -> None:
    session = state.diagnose_session
    assert session is not None and state.diagnose_review_revision is not None and state.diagnose_review_round is not None
    review = DiagnosisReview(reviewed_revision=state.diagnose_review_revision, decision=ReviewDecision.APPROVED, proposal_reviews=[ProposalReview(proposal_id="p", verdict=ProposalReviewVerdict.APPROVE, rationale="supported")])
    session.submit_review(review, round_index=state.diagnose_review_round)


def _revise(state: AgentState) -> None:
    session = state.diagnose_session
    assert session is not None and state.diagnose_review_revision is not None and state.diagnose_review_round is not None
    review = DiagnosisReview(reviewed_revision=state.diagnose_review_revision, decision=ReviewDecision.REVISION_REQUIRED, proposal_reviews=[ProposalReview(proposal_id="p", verdict=ProposalReviewVerdict.REVISE, rationale="need more")])
    session.submit_review(review, round_index=state.diagnose_review_round)


def _rework_and_finalize(state: AgentState) -> None:
    session = state.diagnose_session
    assert session is not None
    proposal = session.claim_proposals["p"]
    session.submit_claim_proposal(
        proposal.model_copy(update={"statement": "workers visibly contend"})
    )
    assert session.request_review()[0]


def _config() -> AgentConfig:
    return AgentConfig(provider=_Provider(), system="s", model="m", max_tokens=1)


@pytest.mark.asyncio
async def test_first_pass_approval_promotes_claim(tmp_path):
    runner = _ScriptedRunner([_prepare_diagnosis, _approve])
    result = await DiagnosisWorkflow(_session(tmp_path), _config(), _config(), DiagnosisReviewPolicy(), NoopTracer(), runner).run("go")
    assert result.status == DiagnosisStatus.INCONCLUSIVE
    assert [claim.id for claim in result.validated_claims] == ["p"]
    assert runner.calls == ["diagnostician", "reviewer"]


@pytest.mark.asyncio
async def test_agent_end_without_finalize_is_incomplete(tmp_path):
    runner = _ScriptedRunner([lambda state: None])
    result = await DiagnosisWorkflow(_session(tmp_path), _config(), _config(), DiagnosisReviewPolicy(), NoopTracer(), runner).run("go")
    assert result.status == DiagnosisStatus.INCOMPLETE


@pytest.mark.asyncio
async def test_reviewer_end_without_submission_is_incomplete(tmp_path):
    runner = _ScriptedRunner([_prepare_diagnosis, lambda state: None])
    result = await DiagnosisWorkflow(_session(tmp_path), _config(), _config(), DiagnosisReviewPolicy(), NoopTracer(), runner).run("go")
    assert result.status == DiagnosisStatus.INCOMPLETE
    assert [proposal.id for proposal in result.claim_proposals] == ["p"]


@pytest.mark.asyncio
async def test_zero_rework_downgrades_unresolved_proposal(tmp_path):
    runner = _ScriptedRunner([_prepare_diagnosis, _revise])
    result = await DiagnosisWorkflow(_session(tmp_path), _config(), _config(), DiagnosisReviewPolicy(max_rework_rounds=0), NoopTracer(), runner).run("go")
    assert result.status == DiagnosisStatus.INCONCLUSIVE
    assert result.validated_claims == []
    assert [claim.id for claim in result.unvalidated_claims] == ["p"]
    assert runner.calls == ["diagnostician", "reviewer"]




@pytest.mark.asyncio
async def test_one_rework_then_second_review_can_approve(tmp_path):
    runner = _ScriptedRunner([_prepare_diagnosis, _revise, _rework_and_finalize, _approve])
    result = await DiagnosisWorkflow(_session(tmp_path), _config(), _config(), DiagnosisReviewPolicy(max_rework_rounds=1), NoopTracer(), runner).run("go")
    assert [claim.id for claim in result.validated_claims] == ["p"]
    assert [cycle.round_index for cycle in result.review_history] == [0, 1]
    assert runner.calls == ["diagnostician", "reviewer", "diagnostician", "reviewer"]


@pytest.mark.asyncio
async def test_disabled_review_never_produces_validated_claim(tmp_path):
    runner = _ScriptedRunner([_prepare_diagnosis])
    result = await DiagnosisWorkflow(_session(tmp_path), _config(), None, DiagnosisReviewPolicy(mode=ReviewMode.DISABLED), NoopTracer(), runner).run("go")
    assert result.validated_claims == []
    assert [claim.id for claim in result.unvalidated_claims] == ["p"]
    assert result.review_complete is False
