"""orchestrator: respx mock Anthropic 纯文本 SSE → query_loop 走 completed。

断言埋点序列 [TURN_START, PROVIDER_REQUEST, STREAM_END, TRANSITION(completed)]。
"""
import asyncio

import httpx
import respx

from core.loop.orchestrator import QueryParams, query_loop
from core.providers.anthropic import AnthropicAdapter
from core.types import AssistantMessage, TextBlock, UserMessage
from telemetry.events import TraceKind
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
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}\n'
    "\n"
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"世界"}}\n'
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
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _params(spy_tracer=None) -> QueryParams:
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    return QueryParams(
        messages=[UserMessage(content="你好")],
        system="be brief",
        model="claude-sonnet-4-6",
        max_tokens=128,
        provider=adapter,
        abort_signal=asyncio.Event(),
    )


@respx.mock
async def test_query_loop_pure_text_completes():
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=ANTHROPIC_SSE))
    spy = SpyTracer()
    out = [m async for m in query_loop(_params(), spy)]

    assts = [m for m in out if isinstance(m, AssistantMessage)]
    assert len(assts) == 1
    first = assts[0].content[0]
    assert isinstance(first, TextBlock)
    assert first.text == "你好世界"
    assert assts[0].stop_reason == "end_turn"


@respx.mock
async def test_query_loop_trace_sequence_completes():
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=ANTHROPIC_SSE))
    spy = SpyTracer()
    async for _ in query_loop(_params(), spy):
        pass

    kinds = [e.kind for e in spy.events]
    assert TraceKind.TURN_START in kinds
    assert TraceKind.PROVIDER_REQUEST in kinds
    assert TraceKind.STREAM_END in kinds
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions, "should emit at least one TRANSITION"
    assert transitions[-1].payload["reason"] == "completed"
