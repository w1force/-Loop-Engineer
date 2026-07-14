"""Anthropic adapter: to_anthropic 转换 + stream(respx mock SSE)→ StreamEvent。

stream 是 Phase 1 端到端直通的核心: parse_sse 只 yield str → json.loads → StreamEvent。
"""
import asyncio

import httpx
import respx

from core.providers.anthropic import AnthropicAdapter, to_anthropic, to_anthropic_tools
from core.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from telemetry.events import TraceKind
from telemetry.tracer import NoopTracer

BASE = "https://api.anthropic.com"


def test_to_anthropic_user_str_content():
    out = to_anthropic([UserMessage(content="你好")])
    assert out == [{"role": "user", "content": "你好"}]


def test_to_anthropic_assistant_tool_use_and_tool_result():
    msgs = [
        AssistantMessage(
            content=[TextBlock(text="ok"), ToolUseBlock(id="c1", name="f", input={"a": 1})]
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="c1", content="done")]),
    ]
    out = to_anthropic(msgs)
    assert out[0] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "c1", "name": "f", "input": {"a": 1}},
        ],
    }
    assert out[1] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "done", "is_error": False}
        ],
    }


def test_to_anthropic_tools_empty_list():
    assert to_anthropic_tools([]) == []


# ── 一段纯文本回答的 Anthropic SSE 流 ──
ANTHROPIC_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}\n'
    "\n"
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
    "\n"
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}\n'
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


class SpyTracer(NoopTracer):
    def __init__(self):
        self.kinds = []

    def emit(self, event):
        self.kinds.append(event.kind)


@respx.mock
async def test_stream_translates_sse_to_stream_events():
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=ANTHROPIC_SSE))
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)

    events = []
    async for evt in adapter.stream(
        messages=[UserMessage(content="hi")],
        system="be brief",
        tools=[],
        model="claude-sonnet-4-6",
        max_tokens=128,
        abort_signal=asyncio.Event(),
        tracer=NoopTracer(),
    ):
        events.append(evt)

    types = [e.type for e in events]
    assert types[0] == "message_start"
    assert "content_block_delta" in types
    assert types[-1] == "message_stop"
    # message_delta: stop_reason 进 delta,usage 进 message(供 aggregate_stream 读取)
    md = [e for e in events if e.type == "message_delta"][0]
    assert md.delta["stop_reason"] == "end_turn"
    assert md.message["usage"]["output_tokens"] == 5


@respx.mock
async def test_stream_emits_provider_request_before_request():
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=ANTHROPIC_SSE))
    spy = SpyTracer()
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    async for _ in adapter.stream(
        messages=[UserMessage(content="hi")],
        system="",
        tools=[],
        model="m",
        max_tokens=128,
        abort_signal=asyncio.Event(),
        tracer=spy,
    ):
        pass
    assert TraceKind.PROVIDER_REQUEST in spy.kinds


# 流中夹 ping 心跳(智谱/Anthropic 都会发)→ 必须跳过,不能撞 type Literal 校验
ANTHROPIC_SSE_WITH_PING = (
    'event: ping\n'
    'data: {"type": "ping"}\n'
    "\n"
    'event: message_start\n'
    'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}\n'
    "\n"
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
    "\n"
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n'
    "\n"
    'event: content_block_stop\n'
    'data: {"type":"content_block_stop","index":0}\n'
    "\n"
    'event: ping\n'
    'data: {"type": "ping"}\n'
    "\n"
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n'
    "\n"
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n'
    "\n"
)


@respx.mock
async def test_stream_ignores_ping_keepalive():
    respx.post(f"{BASE}/v1/messages").mock(
        return_value=httpx.Response(200, text=ANTHROPIC_SSE_WITH_PING)
    )
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    events = [
        e
        async for e in adapter.stream(
            messages=[UserMessage(content="hi")],
            system="",
            tools=[],
            model="m",
            max_tokens=128,
            abort_signal=asyncio.Event(),
            tracer=NoopTracer(),
        )
    ]
    types = [e.type for e in events]
    assert "ping" not in types  # 心跳被跳过
    assert types[-1] == "message_stop"  # 仍正常完成
