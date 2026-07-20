"""stream_turn: async gen —— 中途 yield StreamEvent, 末尾 yield StreamOutcome(整轮)。"""
import asyncio

from pydantic import BaseModel

from core.loop.orchestrator import QueryParams
from core.loop.phases.stream_turn import StreamOutcome, stream_turn
from core.tools import Tool, ToolContext, default_can_use_tool
from core.tool_executor import StreamingToolExecutor
from core.types import AgentState, QueryState, StreamEvent, UserMessage
from telemetry.tracer import NoopTracer


class _In(BaseModel):
    city: str


async def _ok(inp, ctx):
    return inp.city


def _seq_tool_use():
    return [
        StreamEvent(type="message_start"),
        StreamEvent(
            type="content_block_start",
            index=0,
            block={"type": "tool_use", "id": "c1", "name": "get", "input": {}},
        ),
        StreamEvent(
            type="content_block_delta", index=0, delta={"tool_input": '{"city":"X"}'}
        ),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(
            type="message_delta",
            delta={"stop_reason": "tool_use"},
            message={"usage": {"input_tokens": 1, "output_tokens": 1}},
        ),
        StreamEvent(type="message_stop"),
    ]


async def test_stream_turn_gen_yields_stream_events_then_outcome():
    """stream_turn 是 async gen: 中途 yield StreamEvent, 末尾 yield StreamOutcome(含整轮)。"""
    class _FakeProvider:
        def stream(self, **kwargs):
            async def _g():
                for e in _seq_tool_use():   # 复用现有 _seq_tool_use
                    yield e
            return _g()
        def count_tokens(self, messages): return 0

    state = QueryState(messages=[UserMessage(content="hi")])
    agent_state = AgentState(messages=state.messages)
    params = QueryParams(
        system="", model="m", max_tokens=16,
        provider=_FakeProvider(), abort_signal=asyncio.Event(),
    )
    ctx = ToolContext(
        tracer=NoopTracer(), abort_signal=params.abort_signal, agent_state=agent_state,
    )
    executor = StreamingToolExecutor(
        default_can_use_tool, NoopTracer(), ctx,
        [Tool(name="get", description="d", input_model=_In, func=_ok)],
    )

    events = []
    outcome = None
    async for m in stream_turn(agent_state, state, params, NoopTracer(), executor):
        if isinstance(m, StreamOutcome):
            outcome = m
        else:
            events.append(m)   # StreamEvent

    # 中途 yield 了 StreamEvent
    assert any(e.type == "content_block_start" for e in events)
    # 末尾 yield StreamOutcome(整轮 + tool_calls)
    assert outcome is not None
    assert outcome.needs_follow_up is True
    assert [b.name for b in outcome.tool_calls] == ["get"]
    assert len(outcome.assistant_msgs) == 1
    assert outcome.assistant_msgs[0].stop_reason == "tool_use"


def _seq_max_tokens_text():
    """纯 text 输出撞 max_tokens: stop_reason=max_tokens。"""
    return [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0, block={"type": "text", "text": ""}),
        StreamEvent(type="content_block_delta", index=0, delta={"text": "半句话"}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="message_delta",
                    delta={"stop_reason": "max_tokens"},
                    message={"usage": {"input_tokens": 1, "output_tokens": 1}}),
        StreamEvent(type="message_stop"),
    ]


async def test_withheld_max_output_tokens():
    class _FakeProvider:
        def stream(self, **kwargs):
            async def _g():
                for e in _seq_max_tokens_text():
                    yield e
            return _g()
        def count_tokens(self, messages): return 0

    state = QueryState(messages=[UserMessage(content="hi")])
    agent_state = AgentState(messages=state.messages)
    params = QueryParams(
        system="", model="m", max_tokens=16,
        provider=_FakeProvider(), abort_signal=asyncio.Event(),
    )
    outcome = None
    async for m in stream_turn(agent_state, state, params, NoopTracer(), None):
        if isinstance(m, StreamOutcome):
            outcome = m
    assert outcome is not None
    assert outcome.withheld == "max_output_tokens"
    assert outcome.stop_reason == "max_tokens"


async def test_withheld_none_when_end_turn():
    class _FakeProvider:
        def stream(self, **kwargs):
            async def _g():
                for e in [
                    StreamEvent(type="message_start"),
                    StreamEvent(type="content_block_start", index=0, block={"type": "text", "text": ""}),
                    StreamEvent(type="content_block_delta", index=0, delta={"text": "done"}),
                    StreamEvent(type="content_block_stop", index=0),
                    StreamEvent(type="message_delta", delta={"stop_reason": "end_turn"},
                                message={"usage": {"input_tokens": 1, "output_tokens": 1}}),
                    StreamEvent(type="message_stop"),
                ]:
                    yield e
            return _g()
        def count_tokens(self, messages): return 0

    state = QueryState(messages=[UserMessage(content="hi")])
    agent_state = AgentState(messages=state.messages)
    params = QueryParams(
        system="", model="m", max_tokens=16,
        provider=_FakeProvider(), abort_signal=asyncio.Event(),
    )
    outcome = None
    async for m in stream_turn(agent_state, state, params, NoopTracer(), None):
        if isinstance(m, StreamOutcome):
            outcome = m
    assert outcome is not None
    assert outcome.withheld is None
