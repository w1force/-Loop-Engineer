"""锁定 core.types / telemetry.events 的 schema 不变量(声明层防回归)。"""
from telemetry.events import TraceEvent, TraceKind

from core.types import (
    MAX_OUTPUT_TOKENS_RECOVERY_LIMIT,
    ESCALATED_MAX_TOKENS,
    AssistantMessage,
    Continue,
    ContinueReason,
    State,
    StreamEvent,
    Terminal,
    TerminalReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)


def test_blocks_and_usage_construct():
    t = TextBlock(text="hi")
    assert t.type == "text"
    tu = ToolUseBlock(id="c1", name="get_weather", input={"city": "Paris"})
    assert tu.type == "tool_use" and tu.input == {"city": "Paris"}
    tr = ToolResultBlock(tool_use_id="c1", content="sunny")
    assert tr.is_error is False
    u = Usage(input_tokens=10, output_tokens=20)
    assert u.output_tokens == 20


def test_messages_str_and_list_content():
    user_str = UserMessage(content="你好")
    assert user_str.content == "你好" and user_str.role == "user"
    user_list = UserMessage(content=[ToolResultBlock(tool_use_id="c1", content="ok")])
    assert user_list.content[0].type == "tool_result"
    asst = AssistantMessage(
        content=[TextBlock(text="x"), ToolUseBlock(id="c1", name="f", input={})],
        usage=Usage(input_tokens=1, output_tokens=2),
        stop_reason="end_turn",
    )
    assert asst.role == "assistant" and len(asst.content) == 2


def test_state_transition_and_roundtrip():
    s = State(
        messages=[UserMessage(content="hi")],
        turn_count=1,
        transition=Continue(reason=ContinueReason.NEXT_TURN),
    )
    assert s.transition.reason is ContinueReason.NEXT_TURN
    # round-trip: union 消息 + transition 都能 dump 后重建
    dumped = s.model_dump()
    s2 = State(**dumped)
    assert s2.turn_count == 1
    assert isinstance(s2.transition, Continue)
    assert s2.transition.reason is ContinueReason.NEXT_TURN
    assert s2.messages[0].role == "user"


def test_stream_event_fields():
    evt = StreamEvent(type="content_block_start", index=0, block={"type": "text"})
    assert evt.type == "content_block_start" and evt.block["type"] == "text"
    assert StreamEvent(type="message_stop").index is None


def test_enums_and_constants():
    assert ContinueReason.NEXT_TURN.value == "next_turn"
    assert TerminalReason.COMPLETED.value == "completed"
    assert MAX_OUTPUT_TOKENS_RECOVERY_LIMIT == 3
    assert ESCALATED_MAX_TOKENS == 32000
    # Terminal 可带 error
    term = Terminal(reason=TerminalReason.MODEL_ERROR, error="boom")
    assert term.error == "boom"


def test_trace_event_defaults():
    e = TraceEvent(kind=TraceKind.TURN_START, turn=1)
    assert e.depth == 0 and e.payload == {} and e.turn == 1
