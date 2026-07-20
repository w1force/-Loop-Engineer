"""恢复/退出判定责任链引擎 (P2 §5.1 + 健壮性 spec §3.4)。

正常链(rules)基于 outcome.withheld; 错误链(error_rules)基于 ProviderError。
两条链同一套 Decision 模型。handle / handle_error / rule.apply 均为 async。
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from ...types import Continue, QueryState, Terminal, TerminalReason
from ...provider_errors import ProviderError
from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

from ..phases.stream_turn import StreamOutcome


class Decision(BaseModel):
    transition: Continue | Terminal
    next_state: QueryState | None = None  # Continue 时给出重建后的 state


class TransitionRule(Protocol):
    name: str

    def match(self, state: QueryState, outcome: StreamOutcome) -> bool: ...

    async def apply(
        self, state: QueryState, outcome: StreamOutcome, params, tracer: Tracer
    ) -> Decision: ...


class ErrorRule(Protocol):
    name: str

    def match(self, state: QueryState, err: ProviderError) -> bool: ...

    async def apply(
        self, state: QueryState, err: ProviderError, params, tracer: Tracer
    ) -> Decision: ...


class RecoveryChain:
    def __init__(self, rules: list[TransitionRule], error_rules: list[ErrorRule]):
        self.rules = rules
        self.error_rules = error_rules

    async def handle(self, state, outcome, params, tracer: Tracer) -> Decision:
        for rule in self.rules:
            if rule.match(state, outcome):
                tracer.emit(
                    TraceEvent(
                        kind=TraceKind.RECOVERY_ATTEMPT,
                        payload={"rule": rule.name, "withheld": outcome.withheld},
                    )
                )
                return await rule.apply(state, outcome, params, tracer)
        # 兜底:正常完成(理论上 CompletedRule 会兜住,这里防御性返回)
        return Decision(transition=Terminal(reason=TerminalReason.COMPLETED))

    async def handle_error(self, state, err: ProviderError, params, tracer: Tracer) -> Decision:
        for rule in self.error_rules:
            if rule.match(state, err):
                tracer.emit(
                    TraceEvent(
                        kind=TraceKind.RECOVERY_ATTEMPT,
                        payload={"rule": rule.name, "error": type(err).__name__},
                    )
                )
                return await rule.apply(state, err, params, tracer)
        # 兜底:无错误规则匹配 → Terminal(MODEL_ERROR)
        return Decision(transition=Terminal(reason=TerminalReason.MODEL_ERROR, error=str(err)))
