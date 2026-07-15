"""aggregate_stream: 每个 content_block_stop 固化一个 block 级 AssistantMessage。

埋点: content_block_start(tool_use) → TOOL_USE_DETECTED;message_stop → STREAM_END。
usage/stop_reason 不再由 aggregate 组装(由 stream_turn 从 message_delta 取,见 Task 7)。
"""
from core.loop.phases.stream_turn import aggregate_stream
from core.types import AssistantMessage, StreamEvent, TextBlock, ToolUseBlock
from telemetry.events import TraceKind
from telemetry.tracer import NoopTracer


class SpyTracer(NoopTracer):
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


async def _events(*evts):
    for e in evts:
        yield e


def _assts(out):
    return [x for x in out if isinstance(x, AssistantMessage)]


async def test_text_block_yields_block_level_assistant():
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0, block={"type": "text", "text": ""}),
        StreamEvent(type="content_block_delta", index=0, delta={"text": "你好"}),
        StreamEvent(type="content_block_delta", index=0, delta={"text": "世界"}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="message_delta", delta={"stop_reason": "end_turn"},
                    message={"usage": {"input_tokens": 10, "output_tokens": 5}}),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]

    assts = _assts(out)
    assert len(assts) == 1  # 一个 block → 一条 block 级
    assert assts[0].content == [TextBlock(text="你好世界")]
    assert any(e.kind is TraceKind.STREAM_END for e in spy.events)


async def test_tool_use_block_assembled_and_detected():
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0,
                    block={"type": "tool_use", "id": "c1", "name": "get_weather", "input": {}}),
        StreamEvent(type="content_block_delta", index=0, delta={"tool_input": '{"city"'}),
        StreamEvent(type="content_block_delta", index=0, delta={"tool_input": ':"Paris"}'}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]

    assts = _assts(out)
    assert assts[0].content == [ToolUseBlock(id="c1", name="get_weather", input={"city": "Paris"})]
    detected = [e for e in spy.events if e.kind is TraceKind.TOOL_USE_DETECTED]
    assert len(detected) == 1
    assert detected[0].payload["tool_name"] == "get_weather"


async def test_multiple_blocks_yield_multiple_block_level_assistants():
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0, block={"type": "text", "text": ""}),
        StreamEvent(type="content_block_delta", index=0, delta={"text": "a"}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="content_block_start", index=1, block={"type": "text", "text": ""}),
        StreamEvent(type="content_block_delta", index=1, delta={"text": "b"}),
        StreamEvent(type="content_block_stop", index=1),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]
    assts = _assts(out)
    assert len(assts) == 2  # 两个 block → 两条 block 级
