"""RecoveryChain: withheld=None → 走 CompletedRule → Terminal(completed)。"""
from typing import cast

from core.loop.phases.stream_turn import StreamOutcome
from core.loop.recovery.rules import build_recovery_chain
from core.provider_errors import (
    FatalProviderError,
    PromptTooLongError,
    ProviderError,
    TransientProviderError,
)
from core.types import (
    AssistantMessage,
    ContinueReason,
    ESCALATED_MAX_TOKENS,
    State,
    Terminal,
    TerminalReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
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


async def test_no_withheld_falls_through_to_completed():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    decision = await chain.handle(state, _outcome(None), params=None, tracer=NoopTracer())
    assert isinstance(decision.transition, Terminal)
    assert decision.transition.reason is TerminalReason.COMPLETED


async def test_completed_rule_emits_recovery_attempt():
    spy = SpyTracer()
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    await chain.handle(state, _outcome(None), params=None, tracer=spy)
    hits = [
        e
        for e in spy.events
        if e.kind is TraceKind.RECOVERY_ATTEMPT and e.payload.get("rule") == "completed"
    ]
    assert len(hits) == 1


async def test_stub_rules_never_match_when_no_withheld():
    # Phase 1 两条桩规则 match 恒 False(因为 withheld 恒 None),不会命中抛错
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    # 多次 handle 都应安全落到 CompletedRule
    for _ in range(3):
        d = await chain.handle(state, _outcome(None), params=None, tracer=NoopTracer())
        assert d.transition.reason is TerminalReason.COMPLETED


async def test_handle_error_fallback_terminal_model_error():
    """无错误规则匹配时, handle_error 兜底返回 Terminal(MODEL_ERROR)。"""
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    d = await chain.handle_error(
        state, ProviderError("x"), params=None, tracer=NoopTracer())
    assert isinstance(d.transition, Terminal)
    assert d.transition.reason is TerminalReason.MODEL_ERROR


# ── Task 7: MaxOutputTokensRule 升档/续写/耗尽 三档 ──
def _outcome_max_tokens(tool_calls=None) -> StreamOutcome:
    return StreamOutcome(
        assistant_msgs=[AssistantMessage(content=[TextBlock(text="半句")])],
        tool_calls=tool_calls or [],
        needs_follow_up=False,
        withheld="max_output_tokens",
    )


async def test_max_tokens_escalate_first_time():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    d = await chain.handle(state, _outcome_max_tokens(), params=None, tracer=NoopTracer())
    assert d.transition.reason is ContinueReason.MAX_OUTPUT_TOKENS_ESCALATE
    assert d.next_state is not None
    assert d.next_state.max_output_tokens_override == ESCALATED_MAX_TOKENS


async def test_max_tokens_recovery_injects_meta_and_placeholders():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")],
                  max_output_tokens_override=ESCALATED_MAX_TOKENS)  # 已升档 → 进续写
    tc = [ToolUseBlock(id="c1", name="get", input={"x": 1})]
    d = await chain.handle(state, _outcome_max_tokens(tool_calls=tc),
                           params=None, tracer=NoopTracer())
    assert d.transition.reason is ContinueReason.MAX_OUTPUT_TOKENS_RECOVERY
    assert d.next_state is not None
    # 本轮 assistant + 占位 result + meta 三条进历史
    added = d.next_state.messages[-3:]
    assert added[0] == AssistantMessage(content=[TextBlock(text="半句")])
    # 占位 user message: 1 个 is_error tool_result
    placeholder = cast(ToolResultBlock, added[1].content[0])
    assert placeholder.is_error is True
    assert placeholder.tool_use_id == "c1"
    # meta 文本
    assert "Resume directly" in added[2].content
    assert d.next_state.max_output_tokens_recovery_count == 1


async def test_max_tokens_recovery_no_tool_calls_skips_placeholder():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")],
                  max_output_tokens_override=ESCALATED_MAX_TOKENS)
    d = await chain.handle(state, _outcome_max_tokens(tool_calls=[]),
                           params=None, tracer=NoopTracer())
    assert d.next_state is not None
    added = d.next_state.messages[-2:]  # 仅 assistant + meta, 无占位
    assert added[0] == AssistantMessage(content=[TextBlock(text="半句")])
    assert "Resume directly" in added[1].content


async def test_max_tokens_exhausted_after_three_recovery():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")],
                  max_output_tokens_override=ESCALATED_MAX_TOKENS,
                  max_output_tokens_recovery_count=3)  # 已耗尽
    d = await chain.handle(state, _outcome_max_tokens(), params=None, tracer=NoopTracer())
    assert isinstance(d.transition, Terminal)
    assert d.transition.reason is TerminalReason.MODEL_ERROR


# ── Task 8: 错误规则 NetworkRetry / PromptTooLong / ModelError ──

def _state(retry=0):
    return State(messages=[UserMessage(content="hi")], network_retry_count=retry)


async def test_network_retry_under_limit(monkeypatch):
    sleeps = []

    async def _fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", _fake_sleep)
    chain = build_recovery_chain()
    d = await chain.handle_error(
        _state(retry=0), TransientProviderError("x"), params=None, tracer=NoopTracer())
    assert d.transition.reason is ContinueReason.NETWORK_RETRY
    assert d.next_state is not None
    assert d.next_state.network_retry_count == 1
    assert len(sleeps) == 1
    # base * 2^0 = 1.0, +jitter[0,0.5) → 实际退避落在 [1.0, 1.5)
    assert sleeps[0] >= 1.0 and sleeps[0] < 1.5


async def test_network_retry_backoff_doubles(monkeypatch):
    sleeps = []
    async def _fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", _fake_sleep)
    chain = build_recovery_chain()
    # 第三次重试(count=2 → 2^2=4s 基底)
    d = await chain.handle_error(
        _state(retry=2), TransientProviderError("x"), params=None, tracer=NoopTracer())
    assert d.next_state is not None
    assert d.next_state.network_retry_count == 3
    assert sleeps[0] >= 4.0 and sleeps[0] < 4.5


async def test_network_retry_exhausted_terminal(monkeypatch):
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", lambda s: None)
    chain = build_recovery_chain()
    d = await chain.handle_error(
        _state(retry=3), TransientProviderError("x"), params=None, tracer=NoopTracer())
    assert isinstance(d.transition, Terminal)
    assert d.transition.reason is TerminalReason.MODEL_ERROR


async def test_prompt_too_long_terminal():
    chain = build_recovery_chain()
    d = await chain.handle_error(
        _state(), PromptTooLongError("too long", status=400), params=None, tracer=NoopTracer())
    assert isinstance(d.transition, Terminal)
    assert d.transition.reason is TerminalReason.PROMPT_TOO_LONG


async def test_model_error_fatal_terminal():
    chain = build_recovery_chain()
    d = await chain.handle_error(
        _state(), FatalProviderError("boom", status=401), params=None, tracer=NoopTracer())
    assert isinstance(d.transition, Terminal)
    assert d.transition.reason is TerminalReason.MODEL_ERROR
