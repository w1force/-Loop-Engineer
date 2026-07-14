"""各条恢复规则 (P2 §5.2)。

Phase 1 只实现 CompletedRule(无 tool_use → 正常完成的主路径能跑通);
PromptTooLongRule / MaxOutputTokensRule 留桩(Phase 1 withheld 恒 None,永不命中)。
"""
from __future__ import annotations

from ...tools import _not_impl
from ...types import State, Terminal, TerminalReason
from telemetry.tracer import Tracer

from ..phases.stream_turn import StreamOutcome
from .base import Decision, RecoveryChain, TransitionRule


class CompletedRule:
    """兜底:正常完成 → Terminal(COMPLETED)。放责任链最后。"""

    name = "completed"

    def match(self, state: State, outcome: StreamOutcome) -> bool:
        return True  # 兜底

    def apply(self, state, outcome, params, tracer: Tracer) -> Decision:
        return Decision(transition=Terminal(reason=TerminalReason.COMPLETED))


class PromptTooLongRule:
    """withheld=="prompt_too_long": 单次触发式压缩 → 失败 Terminal。Phase 5 实现。"""

    name = "prompt_too_long"

    def match(self, state: State, outcome: StreamOutcome) -> bool:
        return outcome.withheld == "prompt_too_long"  # Phase 1 恒 False

    def apply(self, state, outcome, params, tracer: Tracer) -> Decision:
        _not_impl("prompt_too_long recovery", "Phase 5")


class MaxOutputTokensRule:
    """withheld=="max_output_tokens": 两段式 escalate→recovery(≤3)。Phase 5 实现。"""

    name = "max_output_tokens"

    def match(self, state: State, outcome: StreamOutcome) -> bool:
        return outcome.withheld == "max_output_tokens"  # Phase 1 恒 False

    def apply(self, state, outcome, params, tracer: Tracer) -> Decision:
        _not_impl("max_output_tokens recovery", "Phase 5")


def build_recovery_chain() -> RecoveryChain:
    """链的顺序即优先级(对齐 P1 真实实现)。"""
    return RecoveryChain(
        [
            PromptTooLongRule(),  # withheld=="prompt_too_long"
            MaxOutputTokensRule(),  # withheld=="max_output_tokens"
            CompletedRule(),  # 兜底放最后
        ]
    )
