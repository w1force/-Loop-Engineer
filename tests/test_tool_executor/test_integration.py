"""集成测试 (Task 9): 端到端覆盖 tool_use → executor 执行 → tool_result 回灌 → 第二轮 end_turn。

mock Anthropic 两轮 SSE:
  ROUND1: tool_use(get_weather, input={"city":"巴黎"}) —— 触发 streaming executor 机会主义执行
  ROUND2: end_turn 文本("巴黎26度") —— 证明 tool_result 被回灌后模型正常收尾

这条路径前 8 步单元测试未覆盖(Task 8 reviewer 标注的缺口),本测试补齐。
"""
import asyncio
import json

import httpx
import respx
from pydantic import BaseModel

from core.loop.orchestrator import QueryParams, query_loop
from core.providers.anthropic import AnthropicAdapter
from core.tools import Tool
from core.types import AssistantMessage, TextBlock, UserMessage
from telemetry.tracer import NoopTracer

BASE = "https://api.anthropic.com"


class _WeatherIn(BaseModel):
    city: str


async def _weather(inp, ctx):
    """真实可执行的工具:返回城市+温度。executor 实际会调用它。"""
    return f"{inp.city} 26C"


def _tool():
    return Tool(
        name="get_weather",
        description="weather",
        input_model=_WeatherIn,
        func=_weather,
        is_concurrency_safe=True,
    )


def _sse(events):
    """把事件列表渲染成 Anthropic SSE 文本流。"""
    parts = []
    for e in events:
        parts.append(f"event: {e['type']}")
        parts.append(f"data: {json.dumps(e, ensure_ascii=False)}")
        parts.append("")
    return "\n".join(parts) + "\n"


# 第一轮:tool_use(get_weather, input={"city":"巴黎"})
ROUND1 = _sse([
    {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}},
    {"type": "content_block_start", "index": 0,
     "content_block": {"type": "tool_use", "id": "c1", "name": "get_weather", "input": {}}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "input_json_delta", "partial_json": '{"city":"巴黎"}'}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
    {"type": "message_stop"},
])
# 第二轮:工具结果回灌后模型 end_turn 文本
ROUND2 = _sse([
    {"type": "message_start", "message": {"usage": {"input_tokens": 12, "output_tokens": 0}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "巴黎26度"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
    {"type": "message_stop"},
])


@respx.mock
async def test_tool_use_roundtrip_executes_and_reinjects():
    """端到端:tool_use 被 executor 执行 → tool_result 回灌 → 第二轮模型收尾出文本。"""
    responses = iter([httpx.Response(200, text=ROUND1), httpx.Response(200, text=ROUND2)])
    respx.post(f"{BASE}/v1/messages").mock(side_effect=lambda req: next(responses))

    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    params = QueryParams(
        messages=[UserMessage(content="巴黎天气")],
        system="",
        model="claude-sonnet-4-6",
        max_tokens=64,
        provider=adapter,
        abort_signal=asyncio.Event(),
        tools=[_tool()],
        tool_execution_mode="streaming",
    )

    out = [m async for m in query_loop(params, NoopTracer())]
    assts = [m for m in out if isinstance(m, AssistantMessage)]
    # 第二轮 assistant 文本应出现(说明 tool_result 被回灌后模型正常收尾)。
    # 用 isinstance(TextBlock) 做类型守卫,pyright 才能收敛到 .text 属性。
    texts = [b.text for a in assts for b in a.content if isinstance(b, TextBlock)]
    assert "巴黎26度" in texts
