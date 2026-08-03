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


async def test_truncated_tool_input_falls_back_to_empty_not_raised():
    """input_buf 残缺(max_tokens 截断)→ 兜底成 {} 不抛,emit TOOL_INPUT_MALFORMED 记录原始。"""
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0,
                    block={"type": "tool_use", "id": "c1", "name": "f", "input": {}}),
        StreamEvent(type="content_block_delta", index=0,
                    delta={"tool_input": '{"city": "Par'}),  # 残缺 JSON
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="message_delta", delta={"stop_reason": "max_tokens"}),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]
    assts = _assts(out)
    assert len(assts) == 1  # 不丢弃,固化 input={}
    assert assts[0].content == [ToolUseBlock(id="c1", name="f", input={})]
    malformed = [e for e in spy.events if e.kind is TraceKind.TOOL_INPUT_MALFORMED]
    assert len(malformed) == 1
    assert malformed[0].payload["reason"] == "json_decode_error"
    assert malformed[0].payload["raw_input_buf"] == '{"city": "Par'


async def test_incomplete_tool_use_finalized_with_empty_input_and_emits_malformed():
    """tool_use 收到 content_block_start + delta(累积 input_buf 到一半), 但 provider 漏发
    content_block_stop 就 message_stop (实测 GLM stop_reason=tool_use 偶发)。

    残块走正常固化: yield 一条 AssistantMessage(input 兜底成 {}, 截断的 input_buf 不解析),
    经 stream_turn 喂 executor → model_validate 缺必填字段失败 → is_error tool_result 回喂
    Agent (能重试, 不再静默丢失)。同时 emit TOOL_INPUT_MALFORMED(reason=no_content_block_stop)
    供日志解释 detected 数与 exec_start 数的差。
    """
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0,
                    block={"type": "tool_use", "id": "c1", "name": "CaptureDiagnosisEvidence", "input": {}}),
        StreamEvent(type="content_block_delta", index=0,
                    delta={"tool_input": '{"artifact_ids":["td"]'}),  # 截断的半截 JSON
        # ★ 没有 content_block_stop (provider 漏发), 直接 message_stop
        StreamEvent(type="message_delta", delta={"stop_reason": "tool_use"}),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]

    # 残块固化 (B): yield 一条 AssistantMessage, input={} 兜底 → 喂 executor 会校验失败
    assts = _assts(out)
    assert len(assts) == 1
    assert assts[0].content == [ToolUseBlock(id="c1", name="CaptureDiagnosisEvidence", input={})]
    # detected 仍在 (content_block_start 时 emit)
    detected = [e for e in spy.events if e.kind is TraceKind.TOOL_USE_DETECTED]
    assert len(detected) == 1
    assert detected[0].payload["tool_name"] == "CaptureDiagnosisEvidence"
    # 观测性: emit MALFORMED(reason=no_content_block_stop), 区别于 json_decode_error
    malformed = [e for e in spy.events if e.kind is TraceKind.TOOL_INPUT_MALFORMED]
    assert len(malformed) == 1
    assert malformed[0].payload["reason"] == "no_content_block_stop"
    assert malformed[0].payload["tool_use_id"] == "c1"
    assert malformed[0].payload["raw_input_buf"] == '{"artifact_ids":["td"]'


async def test_incomplete_tool_use_does_not_interfere_with_completed_blocks():
    """一轮里既有完成的 tool_use 又有未完成的: 完成者正常 yield (input 解析), 未完成者
    也固化 (input={} 兜底, B) —— 两者都进 executor, 互不干扰。"""
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        # block 0: 完整 tool_use (有 stop)
        StreamEvent(type="content_block_start", index=0,
                    block={"type": "tool_use", "id": "ok", "name": "get_weather", "input": {}}),
        StreamEvent(type="content_block_delta", index=0, delta={"tool_input": '{"city":"Paris"}'}),
        StreamEvent(type="content_block_stop", index=0),
        # block 1: 未完成 tool_use (漏 stop)
        StreamEvent(type="content_block_start", index=1,
                    block={"type": "tool_use", "id": "bad", "name": "f", "input": {}}),
        StreamEvent(type="content_block_delta", index=1, delta={"tool_input": '{"x":'}),
        StreamEvent(type="message_delta", delta={"stop_reason": "tool_use"}),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]

    assts = _assts(out)
    assert len(assts) == 2  # 完成 block + 残块都固化
    inputs = {b.id: b.input for a in assts for b in a.content if isinstance(b, ToolUseBlock)}
    assert inputs["ok"] == {"city": "Paris"}  # 完成块 input 正常解析
    assert inputs["bad"] == {}  # 残块 input 兜底 {} (executor 会校验失败)
    detected = [e for e in spy.events if e.kind is TraceKind.TOOL_USE_DETECTED]
    assert {e.payload["tool_use_id"] for e in detected} == {"ok", "bad"}
    malformed = [e for e in spy.events if e.kind is TraceKind.TOOL_INPUT_MALFORMED]
    assert len(malformed) == 1
    assert malformed[0].payload["tool_use_id"] == "bad"
    assert malformed[0].payload["reason"] == "no_content_block_stop"
