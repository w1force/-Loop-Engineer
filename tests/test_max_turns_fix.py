"""回归:max_turns 收尾 + 成功判定 bug 修复验证。

- 正常完成:query_loop 末尾 yield Terminal(COMPLETED) → submit 判 success + 有文本。
- 命中 max_turns:query_loop 末尾 yield Terminal(MAX_TURNS) → submit 判 error_max_turns,
  而不再是"最后一条是 tool_result 就假成功、text 为空"。
"""
import asyncio

import httpx
import respx
from pydantic import BaseModel

from core.agent_loop import AgentConfig, submit
from core.loop.orchestrator import QueryParams, query_loop
from core.providers.anthropic import AnthropicAdapter
from core.tools import Tool
from core.types import Terminal, TerminalReason, UserMessage
from telemetry.tracer import NoopTracer

BASE = "https://api.anthropic.com"

# 纯文本 end_turn(正常完成)
TEXT_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"世界"}}\n\n'
    'event: content_block_stop\n'
    'data: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)

# 永远返回 tool_use(逼 loop 一直回灌、循环到 max_turns)
TOOL_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"noop","input":{}}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{}"}}\n\n'
    'event: content_block_stop\n'
    'data: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":3}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)


class _NoopIn(BaseModel):
    pass


async def _noop(inp, ctx):
    return "ok"


def _tool() -> Tool:
    return Tool(name="noop", description="noop", input_model=_NoopIn, func=_noop, is_concurrency_safe=True)


def _adapter() -> AnthropicAdapter:
    return AnthropicAdapter(api_key="k", base_url=BASE)


def _params(tools, max_turns) -> QueryParams:
    return QueryParams(
        messages=[UserMessage(content="go")], system="", model="m", max_tokens=64,
        provider=_adapter(), abort_signal=asyncio.Event(), tools=tools,
        max_turns=max_turns, tool_execution_mode="batch",
    )


# ── query_loop 层:终止 yield Terminal(带原因) ──────────
@respx.mock
async def test_query_loop_completed_no_terminal_signal():
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=TEXT_SSE))
    out = [m async for m in query_loop(_params([], 20), NoopTracer())]
    # 对齐 CC:正常完成不发终止信号 → 交外层 is_result_successful 判定
    assert not any(isinstance(m, Terminal) for m in out)


@respx.mock
async def test_query_loop_max_turns_yields_terminal():
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=TOOL_SSE))
    out = [m async for m in query_loop(_params([_tool()], 2), NoopTracer())]
    terms = [m for m in out if isinstance(m, Terminal)]
    assert terms and terms[-1].reason == TerminalReason.MAX_TURNS


# ── submit 层:按 reason 判定(bug 核心) ─────────────────
@respx.mock
async def test_submit_max_turns_is_error_not_fake_success(tmp_path):
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=TOOL_SSE))
    cfg = AgentConfig(
        provider=_adapter(), system="", model="m", max_tokens=64, max_turns=2,
        tools=[_tool()], transcript_path=str(tmp_path / "t.jsonl"), tool_execution_mode="batch",
    )
    results = [r async for r in submit("go", cfg, NoopTracer())]
    # 修复前:最后一条是 tool_result → 假 success、text=''。修复后:按 MAX_TURNS 判错。
    assert results[-1]["subtype"] == "error_max_turns"


@respx.mock
async def test_submit_completed_is_success_with_text(tmp_path):
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=TEXT_SSE))
    cfg = AgentConfig(
        provider=_adapter(), system="", model="m", max_tokens=64, max_turns=20,
        transcript_path=str(tmp_path / "t.jsonl"),
    )
    results = [r async for r in submit("go", cfg, NoopTracer())]
    assert results[-1]["subtype"] == "success"
    assert results[-1]["text"] == "你好世界"
