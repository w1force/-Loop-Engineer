"""agent_loop: is_result_successful 三路径 + submit 端到端 success + 持久化。

Task 2: submit 签名加 agent_state(messages 走 agent_state.messages 跨 submit 累积)。
"""
import asyncio
import json

import httpx
import respx

from core.agent_loop import AgentConfig, is_result_successful, submit
from core.providers.anthropic import AnthropicAdapter
from core.types import (
    AgentState,
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from telemetry.tracer import NoopTracer

BASE = "https://api.anthropic.com"
ANTHROPIC_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}\n'
    "\n"
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
    "\n"
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好世界"}}\n'
    "\n"
    'event: content_block_stop\n'
    'data: {"type":"content_block_stop","index":0}\n'
    "\n"
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n'
    "\n"
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n'
    "\n"
)


def test_is_result_successful_assistant_text():
    msg = AssistantMessage(content=[TextBlock(text="ok")])
    assert is_result_successful(msg, None) is True


def test_is_result_successful_assistant_tool_use_last_is_not_done():
    msg = AssistantMessage(
        content=[TextBlock(text="x"), ToolUseBlock(id="c1", name="f", input={})]
    )
    assert is_result_successful(msg, None) is False  # 最后是 tool_use,还要继续


def test_is_result_successful_user_tool_result():
    msg = UserMessage(content=[ToolResultBlock(tool_use_id="c1", content="done")])
    assert is_result_successful(msg, None) is True


def test_is_result_successful_stop_reason_fallback():
    assert is_result_successful(None, "end_turn") is True
    assert is_result_successful(None, "tool_use") is False


@respx.mock
async def test_submit_success_writes_transcript(tmp_path):
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=ANTHROPIC_SSE))
    path = tmp_path / "t.jsonl"
    cfg = AgentConfig(
        provider=AnthropicAdapter(api_key="k", base_url=BASE),
        system="be brief",
        model="claude-sonnet-4-6",
        max_tokens=128,
        transcript_path=str(path),
    )
    results = [r async for r in submit("你好", AgentState(), cfg, NoopTracer())]

    assert results[-1]["type"] == "result"
    assert results[-1]["subtype"] == "success"
    assert "你好世界" in results[-1]["text"]
    # transcript 落盘:含 user + assistant
    roles = [json.loads(line)["role"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert "user" in roles and "assistant" in roles


def _assistant_msg(text: str) -> AssistantMessage:
    """最小整轮 AssistantMessage(stop_reason=end_turn 视为成功)。"""
    return AssistantMessage(content=[TextBlock(text=text)], stop_reason="end_turn")


class _NoopProvider:
    """空 provider stub: query_loop 被 mock 时不会被真正调用。"""

    def stream(self, **kwargs):  # pragma: no cover - 仅满足 QueryParams 类型
        raise NotImplementedError

    def count_tokens(self, messages) -> int:  # pragma: no cover
        return 0


async def test_submit_handles_tombstone_and_stream_event(monkeypatch, tmp_path):
    """submit 对 Tombstone(不 append)和 StreamEvent(留空 continue)都有显式分支, 不崩。"""
    from core.agent_loop import AgentConfig, submit
    from core.types import Tombstone, StreamEvent

    # mock query_loop 产出: StreamEvent + Tombstone + AssistantMessage
    async def _fake_query_loop(agent_state, params, tracer):
        yield StreamEvent(type="message_start")
        yield Tombstone(turn_id=1)              # 模拟第一轮失败
        yield _assistant_msg("ok")              # 模拟重试轮整轮

    monkeypatch.setattr("core.agent_loop.query_loop", _fake_query_loop)

    provider = _NoopProvider()
    config = AgentConfig(
        provider=provider,
        system="",
        model="m",
        max_tokens=16,
        transcript_path=str(tmp_path / "t.jsonl"),
    )
    results = [r async for r in submit("hi", AgentState(), config, NoopTracer())]
    # submit 不因 Tombstone/StreamEvent 崩, 最终 success
    assert any(r.get("type") == "result" for r in results)
    assert results[-1]["subtype"] == "success"


async def test_submit_accumulates_across_submits(monkeypatch, tmp_path):
    """同一 agent_state 跨两次 submit:messages 累积 + budget 累积。"""
    import core.agent_loop as al
    from core.agent_loop import AgentConfig, build_agent_state, submit
    from core.types import AssistantMessage, TextBlock, Usage
    from telemetry.tracer import NoopTracer

    async def _fake_query_loop(agent_state, params, tracer):
        # 直接 yield 一条 AssistantMessage(带 usage),不依赖 provider/aggregate_stream 协议。
        # query_loop 真实契约(orchestrator.py:152):state.messages.extend(outcome.assistant_msgs)
        # 因 QueryState.messages 与 agent_state.messages 引用同一 list,故 mock 必须 extend
        # agent_state.messages —— 这是 submit 跨 submit 累积的物理机制,不能省。
        msg = AssistantMessage(
            content=[TextBlock(text="reply")],
            usage=Usage(input_tokens=10, output_tokens=5),
            stop_reason="end_turn",
        )
        agent_state.messages.append(msg)
        yield msg
    monkeypatch.setattr(al, "query_loop", _fake_query_loop)

    cfg = AgentConfig(provider=_NoopProvider(), system="base", model="m",
                      max_tokens=100, transcript_path=str(tmp_path / "t.jsonl"))
    astate = build_agent_state(cfg)
    tracer = NoopTracer()

    r1 = [r async for r in submit("hi1", astate, cfg, tracer)]
    r2 = [r async for r in submit("hi2", astate, cfg, tracer)]

    # messages 累积:user1 + assistant1 + user2 + assistant2 = 4
    assert len(astate.messages) == 4
    # budget 累积(两次 submit 各 10 input + 5 output)
    assert astate.total_input_tokens == 20
    assert astate.total_output_tokens == 10
    assert r1[-1]["subtype"] == "success" and r2[-1]["subtype"] == "success"
