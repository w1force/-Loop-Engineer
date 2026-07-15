"""stream_turn: block 级 tool_use → executor.add_tool;组装整轮;yielded 不含 block 级。"""
import asyncio

from pydantic import BaseModel

from core.loop.orchestrator import QueryParams
from core.loop.phases.stream_turn import stream_turn
from core.tools import Tool, ToolContext, default_can_use_tool
from core.tool_executor import StreamingToolExecutor
from core.types import AssistantMessage, State, StreamEvent, UserMessage
from telemetry.tracer import NoopTracer


class _In(BaseModel):
    city: str


async def _ok(inp, ctx):
    return {"w": inp.city}


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


async def _fake_events():
    for e in _seq_tool_use():
        yield e


async def test_stream_turn_feeds_executor_and_assembles_full_turn(monkeypatch):
    # 用 _FakeProvider.stream 直接返回固定事件流,绕过 SSE 解析聚焦 stream_turn 逻辑
    class _FakeProvider:
        def stream(self, **kwargs):
            return _fake_events()

        def count_tokens(self, messages):
            return 0

    state = State(messages=[UserMessage(content="hi")])
    params = QueryParams(
        messages=state.messages,
        system="",
        model="m",
        max_tokens=16,
        provider=_FakeProvider(),
        abort_signal=asyncio.Event(),
    )
    ctx = ToolContext(tracer=NoopTracer(), abort_signal=params.abort_signal)
    executor = StreamingToolExecutor(
        default_can_use_tool,
        NoopTracer(),
        ctx,
        [Tool(name="get", description="d", input_model=_In, func=_ok)],
    )
    outcome = await stream_turn(state, params, NoopTracer(), executor)

    # add_tool 已喂给 executor: 收到 tool_use block
    assert outcome.needs_follow_up is True
    assert [b.name for b in outcome.tool_calls] == ["get"]

    # 整轮 assistant 仍是一条,带 usage/stop_reason
    assert len(outcome.assistant_msgs) == 1
    assert outcome.assistant_msgs[0].stop_reason == "tool_use"
    usage = outcome.assistant_msgs[0].usage
    assert usage is not None and usage.output_tokens == 1

    # yielded 不含 block 级(只有原始 StreamEvent + 末尾整轮)
    assts_in_yielded = [m for m in outcome.yielded if isinstance(m, AssistantMessage)]
    assert len(assts_in_yielded) == 1
