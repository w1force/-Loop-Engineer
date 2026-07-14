"""aggregate_stream: 流事件 → 固化 AssistantMessage(红线#4: 先攒齐再 yield)。

埋点: content_block_start(tool_use) → TOOL_USE_DETECTED;message_stop → STREAM_END。
"""
from core.loop.phases.stream_turn import aggregate_stream
from core.types import (
    AssistantMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    Usage,
)
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


async def test_pure_text_aggregates_to_one_assistant_message():
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0, block={"type": "text", "text": ""}),
        StreamEvent(type="content_block_delta", index=0, delta={"text": "你好"}),
        StreamEvent(type="content_block_delta", index=0, delta={"text": "世界"}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(
            type="message_delta",
            delta={"stop_reason": "end_turn"},
            message={"usage": {"input_tokens": 10, "output_tokens": 5}},
        ),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]

    asst = out[-1]
    assert isinstance(asst, AssistantMessage)
    assert asst.content == [TextBlock(text="你好世界")]
    assert asst.stop_reason == "end_turn"
    assert asst.usage == Usage(input_tokens=10, output_tokens=5)
    assert any(e.kind is TraceKind.STREAM_END for e in spy.events)


async def test_tool_use_detected_and_input_assembled():
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(
            type="content_block_start",
            index=0,
            block={"type": "tool_use", "id": "c1", "name": "get_weather", "input": {}},
        ),
        StreamEvent(type="content_block_delta", index=0, delta={"tool_input": '{"city"'}),
        StreamEvent(type="content_block_delta", index=0, delta={"tool_input": ':"Paris"}'}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(
            type="message_delta",
            delta={"stop_reason": "tool_use"},
            message={"usage": {"input_tokens": 8, "output_tokens": 2}},
        ),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]

    asst = out[-1]
    assert isinstance(asst, AssistantMessage)
    assert asst.content == [ToolUseBlock(id="c1", name="get_weather", input={"city": "Paris"})]
    detected = [e for e in spy.events if e.kind is TraceKind.TOOL_USE_DETECTED]
    assert len(detected) == 1
    assert detected[0].payload["tool_name"] == "get_weather"
    assert detected[0].payload["tool_use_id"] == "c1"


async def test_thinking_block_collected_alongside_text():
    # 思考模型(glm-5.1 等):thinking 块 + 正式 text 块共存
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(
            type="content_block_start", index=0, block={"type": "thinking", "thinking": ""}
        ),
        StreamEvent(
            type="content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "让我想想"}
        ),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="content_block_start", index=1, block={"type": "text", "text": ""}),
        StreamEvent(type="content_block_delta", index=1, delta={"text": "答案是42"}),
        StreamEvent(type="content_block_stop", index=1),
        StreamEvent(
            type="message_delta",
            delta={"stop_reason": "end_turn"},
            message={"usage": {"input_tokens": 1, "output_tokens": 2}},
        ),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]
    asst = out[-1]
    assert isinstance(asst, AssistantMessage)
    texts = [b.text for b in asst.content if isinstance(b, TextBlock)]
    assert "让我想想" in texts
    assert "答案是42" in texts


async def test_redline4_only_one_assistant_with_final_usage():
    # message_delta 在 message_stop 前:usage/stop_reason 暂存,只在 message_stop yield 最终对象
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0, block={"type": "text", "text": ""}),
        StreamEvent(type="content_block_delta", index=0, delta={"text": "hi"}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(
            type="message_delta",
            delta={"stop_reason": "end_turn"},
            message={"usage": {"input_tokens": 1, "output_tokens": 99}},
        ),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]
    assts = [x for x in out if isinstance(x, AssistantMessage)]
    assert len(assts) == 1  # 不在 content_block_stop 时 yield 占位再 mutate
    assert assts[0].usage is not None
    assert assts[0].usage.output_tokens == 99
