"""恢复/退出判定责任链引擎 (P2 §5.1)。

`!needs_follow_up` 之后的"恢复/退出判定"是一串规则竞争同一个 withheld 状态,
谁能处理谁返回——名副其实的责任链。拆成一串 TransitionRule,可读/可测性大涨。
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from ...types import Continue, State, Terminal, TerminalReason
from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

from ..phases.stream_turn import StreamOutcome


class Decision(BaseModel):
    transition: Continue | Terminal
    next_state: State | None = None  # Continue 时给出重建后的 state


class TransitionRule(Protocol):
    name: str

    def match(self, state: State, outcome: StreamOutcome) -> bool: ...

    def apply(
        self, state: State, outcome: StreamOutcome, params, tracer: Tracer
    ) -> Decision: ...


class RecoveryChain:
    def __init__(self, rules: list[TransitionRule]):
        self.rules = rules

    def handle(self, state, outcome, params, tracer: Tracer) -> Decision:
        for rule in self.rules:
            if rule.match(state, outcome):
                tracer.emit(
                    TraceEvent(
                        kind=TraceKind.RECOVERY_ATTEMPT,
                        payload={"rule": rule.name, "withheld_kind": outcome.withheld},
                    )
                )
                return rule.apply(state, outcome, params, tracer)
        # 兜底:正常完成(理论上 CompletedRule 会兜住,这里防御性返回)
        return Decision(transition=Terminal(reason=TerminalReason.COMPLETED))
