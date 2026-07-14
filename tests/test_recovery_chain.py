"""RecoveryChain: withheld=None → 走 CompletedRule → Terminal(completed)。"""
from core.loop.phases.stream_turn import StreamOutcome
from core.loop.recovery.rules import build_recovery_chain
from core.types import State, Terminal, TerminalReason, UserMessage
from telemetry.events import TraceKind
from telemetry.tracer import NoopTracer


class SpyTracer(NoopTracer):
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _outcome(withheld=None) -> StreamOutcome:
    return StreamOutcome(
        assistant_msgs=[], tool_calls=[], needs_follow_up=False, withheld=withheld
    )


def test_no_withheld_falls_through_to_completed():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    decision = chain.handle(state, _outcome(None), params=None, tracer=NoopTracer())
    assert isinstance(decision.transition, Terminal)
    assert decision.transition.reason is TerminalReason.COMPLETED


def test_completed_rule_emits_recovery_attempt():
    spy = SpyTracer()
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    chain.handle(state, _outcome(None), params=None, tracer=spy)
    hits = [
        e
        for e in spy.events
        if e.kind is TraceKind.RECOVERY_ATTEMPT and e.payload.get("rule") == "completed"
    ]
    assert len(hits) == 1


def test_stub_rules_never_match_when_no_withheld():
    # Phase 1 两条桩规则 match 恒 False(因为 withheld 恒 None),不会命中抛错
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    # 多次 handle 都应安全落到 CompletedRule
    for _ in range(3):
        d = chain.handle(state, _outcome(None), params=None, tracer=NoopTracer())
        assert d.transition.reason is TerminalReason.COMPLETED
