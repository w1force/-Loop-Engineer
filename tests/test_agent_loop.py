"""agent_loop: is_result_successful 三路径 + submit 端到端 success + 持久化。"""
import asyncio
import json

import httpx
import respx

from core.agent_loop import AgentConfig, is_result_successful, submit
from core.providers.anthropic import AnthropicAdapter
from core.types import (
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
    results = [r async for r in submit("你好", cfg, NoopTracer())]

    assert results[-1]["type"] == "result"
    assert results[-1]["subtype"] == "success"
    assert "你好世界" in results[-1]["text"]
    # transcript 落盘:含 user + assistant
    roles = [json.loads(line)["role"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert "user" in roles and "assistant" in roles


@respx.mock
async def test_submit_streams_text_deltas(tmp_path):
    """submit 应把 text delta 实时 yield 给调用方,而非等整条回答完成。"""
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=ANTHROPIC_SSE))
    path = tmp_path / "t.jsonl"
    cfg = AgentConfig(
        provider=AnthropicAdapter(api_key="k", base_url=BASE),
        system="be brief",
        model="claude-sonnet-4-6",
        max_tokens=128,
        transcript_path=str(path),
    )
    chunks = [c async for c in submit("你好", cfg, NoopTracer())]
    text_chunks = [c["text"] for c in chunks if c.get("type") == "text"]
    assert "".join(text_chunks) == "你好世界"  # 流式增量拼回完整回答
    assert chunks[-1]["type"] == "result"
    assert chunks[-1]["subtype"] == "success"
