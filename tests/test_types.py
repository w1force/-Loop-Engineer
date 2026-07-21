"""锁定 core.types / telemetry.events 的 schema 不变量(声明层防回归)。"""
from telemetry.events import TraceEvent, TraceKind

from core.types import (
    MAX_OUTPUT_TOKENS_RECOVERY_LIMIT,
    ESCALATED_MAX_TOKENS,
    AssistantMessage,
    Continue,
    ContinueReason,
    QueryState,
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
    s = QueryState(
        messages=[UserMessage(content="hi")],
        turn_count=1,
        transition=Continue(reason=ContinueReason.NEXT_TURN),
    )
    assert s.transition.reason is ContinueReason.NEXT_TURN
    # round-trip: union 消息 + transition 都能 dump 后重建
    dumped = s.model_dump()
    s2 = QueryState(**dumped)
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
    assert ESCALATED_MAX_TOKENS == 64_000
    # Terminal 可带 error
    term = Terminal(reason=TerminalReason.MODEL_ERROR, error="boom")
    assert term.error == "boom"


def test_trace_event_defaults():
    e = TraceEvent(kind=TraceKind.TURN_START, turn=1)
    assert e.depth == 0 and e.payload == {} and e.turn == 1


# ── Task 2: 网络重试 / 用户中断 / 计数字段 / 上限常量 ──
def test_network_retry_continue_reason_exists():
    assert ContinueReason.NETWORK_RETRY.value == "network_retry"


def test_user_interrupt_replaces_aborted():
    assert TerminalReason.USER_INTERRUPT.value == "user_interrupt"
    assert not hasattr(TerminalReason, "ABORTED")


def test_state_network_retry_count_defaults_zero():
    s = QueryState(messages=[UserMessage(content="hi")])
    assert s.network_retry_count == 0


def test_escalated_max_tokens_is_64000():
    assert ESCALATED_MAX_TOKENS == 64_000


# ── Task 1: Tombstone(流式失败通知下游) ──
from core.types import Tombstone


def test_tombstone_holds_turn_id():
    t = Tombstone(turn_id=3)
    assert t.turn_id == 3


# ── agent_state 重构 Task 1: AgentState / QueryState 改名 / SkillMeta 移入 ──
from pathlib import Path  # noqa: E402

from core.types import AgentState, SkillMeta  # noqa: E402


def test_agent_state_defaults():
    a = AgentState()
    assert a.messages == []
    assert a.skills == []
    assert a.total_input_tokens == 0
    assert a.total_output_tokens == 0
    assert a.cwd == ""


def test_query_state_keeps_messages():
    q = QueryState(messages=[UserMessage(content="hi")])
    assert q.turn_count == 1
    assert len(q.messages) == 1


def test_skill_meta_in_types():
    m = SkillMeta(name="x", description="d", skill_dir=Path("/x"), skill_md=Path("/x/SKILL.md"))
    assert m.name == "x"
