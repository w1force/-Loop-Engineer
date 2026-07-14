"""桩扩展点验证(DoD): tool_use 路径触发 run_tools 桩;OpenAI×2 stream 桩。

证明 Phase 2/4 的扩展点已就位 —— 接口稳定,运行到桩才抛 NotImplementedError。
"""
import asyncio
import json

import httpx
import pytest
import respx

from core.agent_loop import AgentConfig, submit
from core.providers.anthropic import AnthropicAdapter
from core.providers.openai_chat import OpenAIChatAdapter
from core.providers.openai_responses import OpenAIResponsesAdapter
from core.types import UserMessage
from telemetry.tracer import NoopTracer

BASE = "https://api.anthropic.com"


def _sse(events: list[dict]) -> str:
    parts = []
    for evt in events:
        parts.append(f"event: {evt['type']}")
        parts.append(f"data: {json.dumps(evt, ensure_ascii=False)}")
        parts.append("")
    return "\n".join(parts) + "\n"


# 一段 tool_use 的 Anthropic SSE(arguments 分片到达,需累积后才能 json.loads)
TOOL_USE_SSE = _sse(
    [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "c1", "name": "get_weather", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 10}},
        {"type": "message_stop"},
    ]
)


@respx.mock
async def test_tool_use_path_triggers_run_tools_stub():
    """模型返回 tool_use → needs_follow_up → execute_tools_phase → run_tools 桩抛错。"""
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=TOOL_USE_SSE))
    cfg = AgentConfig(
        provider=AnthropicAdapter(api_key="k", base_url=BASE),
        system="",
        model="claude-sonnet-4-6",
        max_tokens=128,
        transcript_path="run.test.jsonl",
    )
    with pytest.raises(NotImplementedError, match="tool execution"):
        async for _ in submit("查一下巴黎天气", cfg, NoopTracer()):
            pass


async def test_openai_chat_adapter_stream_is_phase4_stub():
    adapter = OpenAIChatAdapter(api_key="k")
    with pytest.raises(NotImplementedError, match="OpenAI chat stream"):
        async for _ in adapter.stream(
            messages=[UserMessage(content="hi")],
            system="",
            tools=[],
            model="gpt-4o",
            max_tokens=128,
            abort_signal=asyncio.Event(),
            tracer=NoopTracer(),
        ):
            pass


async def test_openai_responses_adapter_stream_is_phase4_stub():
    adapter = OpenAIResponsesAdapter(api_key="k")
    with pytest.raises(NotImplementedError, match="OpenAI responses stream"):
        async for _ in adapter.stream(
            messages=[UserMessage(content="hi")],
            system="",
            tools=[],
            model="gpt-4o",
            max_tokens=128,
            abort_signal=asyncio.Event(),
            tracer=NoopTracer(),
        ):
            pass
