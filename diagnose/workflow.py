"""可配置的诊断、独立审查与有界返工编排。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from core.agent_loop import (
    AgentConfig,
    build_agent_state,
    shutdown_agent_state,
    submit,
)
from core.types import AgentState
from diagnose.model import (
    Claim,
    ClaimStatus,
    DiagnosisResult,
    DiagnosisReviewPolicy,
    DiagnosisStatus,
    ReviewDecision,
    ReviewMode,
    UnresolvedReviewAction,
)
from diagnose.session import DiagnosisSession
from telemetry.tracer import Tracer


@dataclass
class DiagnosisWorkflow:
    session: DiagnosisSession
    diagnosis_config: AgentConfig
    review_config: AgentConfig | None
    review_policy: DiagnosisReviewPolicy
    tracer: Tracer

    async def run(self, prompt: str) -> DiagnosisResult:
        diagnosis_state = build_agent_state(self.diagnosis_config)
        diagnosis_state.diagnose_session = self.session
        diagnosis_state.diagnose_actor = "diagnostician"
        try:
            # 禁用评审:诊断 agent 跑完直接定稿(无需 reviewer / 返工)。
            if self.review_policy.mode == ReviewMode.DISABLED:
                await self._drain(prompt, diagnosis_state, self.diagnosis_config)
                if self.session.review_requested_revision is None:
                    return self._incomplete("diagnosis Agent ended without a successful FinalizeDiagnosis call")
                self.session.finalize_without_review()
                return self.session.build_result()

            # 需要评审但没配 reviewer:提前失败,不白跑诊断。
            if self.review_config is None:
                return self._incomplete("review is required but no review Agent config was provided")

            # 诊断 → 评审 → (未通过则返工再诊断)… 最多 max_rework_rounds+1 轮。
            # 第 0 轮用初始 prompt;返工轮用 _rework_prompt 把上轮评审意见回灌给诊断 agent。
            next_prompt = prompt
            for round_index in range(self.review_policy.max_rework_rounds + 1):
                await self._drain(next_prompt, diagnosis_state, self.diagnosis_config)
                if self.session.review_requested_revision is None:
                    return self._incomplete("diagnosis Agent ended without a successful FinalizeDiagnosis call")

                review = await self._run_review(round_index)
                if review is None:
                    return self._incomplete("review Agent ended without SubmitDiagnosisReview")

                if review.decision == ReviewDecision.APPROVED:
                    self.session.apply_review(
                        review,
                        unresolved_action=self.review_policy.unresolved_action,
                        finalize_unresolved=False,
                    )
                    allowed, reasons = self.session.finalize_gate()
                    if not allowed:
                        return self._incomplete("final gate rejected approved review: " + "; ".join(reasons))
                    return self.session.build_result()

                # 未通过且已达返工上限:按 unresolved_action 降级收尾。
                if round_index >= self.review_policy.max_rework_rounds:
                    self.session.apply_review(
                        review,
                        unresolved_action=self.review_policy.unresolved_action,
                        finalize_unresolved=True,
                    )
                    if self.review_policy.unresolved_action == UnresolvedReviewAction.FAIL:
                        return self._incomplete("diagnosis review remained unresolved after maximum rework")
                    allowed, reasons = self.session.finalize_gate()
                    if not allowed:
                        return self._incomplete("final gate rejected downgraded review: " + "; ".join(reasons))
                    return self.session.build_result()

                # 还有返工预算:把评审意见回灌给诊断 agent,下一轮重跑诊断。
                next_prompt = self._rework_prompt(round_index)

            return self._incomplete("diagnosis workflow exhausted unexpectedly")
        finally:
            await shutdown_agent_state(diagnosis_state)

    async def _run_review(self, round_index: int):
        assert self.review_config is not None
        state = build_agent_state(self.review_config)
        state.diagnose_session = self.session
        state.diagnose_actor = "reviewer"
        state.diagnose_review_round = round_index
        state.diagnose_review_revision = self.session.review_requested_revision
        history_len = len(self.session.review_history)
        try:
            await self._drain(
                f"Review diagnosis revision {state.diagnose_review_revision} independently.",
                state,
                self.review_config,
            )
        finally:
            await shutdown_agent_state(state)
        if len(self.session.review_history) != history_len + 1:
            return None
        return self.session.review_history[-1].review

    async def _drain(self, prompt: str, state: AgentState, config: AgentConfig) -> None:
        async for _ in submit(prompt, state, config, self.tracer):
            pass

    def _rework_prompt(self, round_index: int) -> str:
        review = self.session.review_history[-1].review
        payload = json.dumps(review.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        return (
            "<system-reminder>Independent review requires revision. Address every ERROR finding, "
            "update evidence, hypotheses, and proposals as needed, then call FinalizeDiagnosis again."
            "</system-reminder>\n"
            f'<diagnosis-review revision="{review.reviewed_revision}" round="{round_index}">{payload}</diagnosis-review>'
        )

    def _incomplete(self, reason: str) -> DiagnosisResult:
        self.session.mark_incomplete(reason)
        result = self.session.build_result()
        return result.model_copy(update={"status": DiagnosisStatus.INCOMPLETE})
