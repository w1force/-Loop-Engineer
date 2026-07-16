"""外层 agent_loop (P1 §7 简化版,无 UI)。

只做三件事: 持久化(transcript) + 全局守卫(预算) + 收尾判定(success/error)。
不直接调 LLM —— 交给内层 query_loop。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Callable, Literal

from .builtin_tools import builtin_tools
from .builtin_tools.readstate import FileReadState
from .loop.orchestrator import QueryParams, query_loop
from .provider import Provider
from .tools import Tool, default_can_use_tool
from .transcript import record_transcript
from .types import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)
from telemetry.tracer import Tracer


@dataclass
class AgentConfig:
    provider: Provider
    system: str | list[dict]
    model: str
    max_tokens: int
    abort_signal: asyncio.Event = field(default_factory=asyncio.Event)
    max_turns: int = 20
    initial_messages: list[Message] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)
    can_use_tool: Callable = default_can_use_tool
    max_budget_usd: float | None = None
    transcript_path: str = "transcript.jsonl"
    tool_execution_mode: Literal["streaming", "batch"] = "streaming"


def is_result_successful(msg, stop_reason: str | None) -> bool:
    """三条通过路径(照搬本项目 queryHelpers.ts:56)。"""
    if isinstance(msg, AssistantMessage):
        last = msg.content[-1] if msg.content else None
        return last is not None and last.type in ("text", "thinking", "redacted_thinking")
    if isinstance(msg, UserMessage) and isinstance(msg.content, list) and msg.content:
        return all(b.type == "tool_result" for b in msg.content)
    return stop_reason == "end_turn"


def _last_message(messages: list[Message], roles: tuple[str, ...]):
    for m in reversed(messages):
        if m.role in roles:
            return m
    return None


def _extract_text(msg) -> str:
    if isinstance(msg, AssistantMessage):
        return "".join(b.text for b in msg.content if isinstance(b, TextBlock))
    return ""


def _rough_cost(input_tokens: int, output_tokens: int) -> float:
    # 占位估算($3/M input + $15/M output 量级);Phase 6 再精确化
    return (input_tokens * 3 + output_tokens * 15) / 1_000_000


async def submit(
    prompt: str, config: AgentConfig, tracer: Tracer
) -> AsyncIterator[dict]:
    """外层 agent loop。进 loop 前先落盘(红线#5),收尾判定 yield result。"""
    messages: list[Message] = [*config.initial_messages, UserMessage(content=prompt)]
    await record_transcript(messages, config.transcript_path)  # 红线#5

    # agent 级 FileReadState: read/write 工具工厂闭包共享同一实例(陈旧检测前提)。
    # 追加到 config.tools 之后——调用者提供的工具与 builtin 工具共存(spec §3.3 方案 A, 不改 query_loop)。
    read_state = FileReadState()
    tools = [*config.tools, *builtin_tools(read_state)]

    params = QueryParams(
        messages=messages,
        system=config.system,
        model=config.model,
        max_tokens=config.max_tokens,
        provider=config.provider,
        abort_signal=config.abort_signal,
        tools=tools,
        max_turns=config.max_turns,
        can_use_tool=config.can_use_tool,
        tool_execution_mode=config.tool_execution_mode,
    )

    last_stop_reason: str | None = None
    total_in = total_out = 0
    async for msg in query_loop(params, tracer):
        if isinstance(msg, AssistantMessage):
            messages.append(msg)
            await record_transcript(messages, config.transcript_path)
            last_stop_reason = msg.stop_reason
            if msg.usage:
                total_in += msg.usage.input_tokens
                total_out += msg.usage.output_tokens
        elif isinstance(msg, UserMessage):
            messages.append(msg)
            await record_transcript(messages, config.transcript_path)
        # StreamEvent: 无 UI,忽略(不持久化)

        if config.max_budget_usd is not None:
            if _rough_cost(total_in, total_out) >= config.max_budget_usd:
                yield {"type": "result", "subtype": "error_budget", "error": "budget exceeded"}
                return

    result = _last_message(messages, ("assistant", "user"))
    if not is_result_successful(result, last_stop_reason):
        yield {"type": "result", "subtype": "error_during_execution"}
        return
    yield {
        "type": "result",
        "subtype": "success",
        "text": _extract_text(result),
        "usage": {"input_tokens": total_in, "output_tokens": total_out},
    }
