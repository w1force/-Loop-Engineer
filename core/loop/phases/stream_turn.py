"""phase: 流式调 LLM + aggregate_stream(边聚合边打点)。

aggregate_stream 在 P1 §6 逻辑基础上只加 tracer.emit,不改逻辑(P2 §3.5)。
红线#4: usage/stop_reason 暂存,等 message_stop 一次性组装最终 AssistantMessage 再 yield
(Python AsyncGenerator yield 后 mutate 不安全,与 TS 版的关键差异)。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ...types import (
    AssistantMessage,
    State,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

if TYPE_CHECKING:
    from ..orchestrator import QueryParams


def _to_block(b: dict):
    t = b.get("type")
    if t == "text":
        return TextBlock(text=b.get("text", ""))
    if t == "tool_use":
        return ToolUseBlock(
            id=b.get("id", ""), name=b.get("name", ""), input=b.get("input", {})
        )
    if t == "thinking":
        return TextBlock(text=b.get("thinking", ""))
    if t == "redacted_thinking":
        return TextBlock(text="[redacted thinking]")
    # 未知块:空文本兜底,绝不把 dict 字符串化混进答案
    return TextBlock(text="")


async def aggregate_stream(
    events: AsyncIterator[StreamEvent], tracer: Tracer
) -> AsyncIterator[StreamEvent | AssistantMessage]:
    """消费 provider 事件流,聚合出固化的 AssistantMessage。"""
    blocks: dict[int, dict] = {}
    usage = Usage()
    stop_reason: str | None = None
    async for evt in events:
        yield evt  # 原事件透传给外层
        if evt.type == "content_block_start":
            blocks[evt.index] = dict(evt.block or {})
            if (evt.block or {}).get("type") == "tool_use":  # ★ TOOL_USE_DETECTED
                tracer.emit(
                    TraceEvent(
                        kind=TraceKind.TOOL_USE_DETECTED,
                        payload={
                            "tool_name": (evt.block or {}).get("name"),
                            "tool_use_id": (evt.block or {}).get("id"),
                            "index": evt.index,
                        },
                    )
                )
        elif evt.type == "content_block_delta":
            b = blocks[evt.index]
            d = evt.delta or {}
            if "text" in d:
                b["text"] = b.get("text", "") + d["text"]
            if "tool_input" in d:  # 累积 JSON 字符串,勿中途解析(红线#1)
                b["input_buf"] = b.get("input_buf", "") + d["tool_input"]
            if "thinking" in d:  # thinking_delta:累积思考内容(思考模型)
                b["thinking"] = b.get("thinking", "") + d["thinking"]
        elif evt.type == "content_block_stop":
            b = blocks[evt.index]
            if b.get("type") == "tool_use":
                b["input"] = json.loads(b.pop("input_buf", "") or "{}")
        elif evt.type == "message_delta":  # 暂存,不写进已 yield 的对象(红线#4)
            stop_reason = (evt.delta or {}).get("stop_reason", stop_reason)
            if evt.message and "usage" in evt.message:
                usage = Usage(**evt.message["usage"])
        elif evt.type == "message_stop":
            tracer.emit(
                TraceEvent(
                    kind=TraceKind.STREAM_END,
                    payload={"stop_reason": stop_reason, "usage": usage.model_dump()},
                )
            )
            content = [_to_block(b) for _, b in sorted(blocks.items())]
            yield AssistantMessage(content=content, usage=usage, stop_reason=stop_reason)


class StreamOutcome(BaseModel):
    """phase 之间传递的中间结果(避免 phase 直接改 state)。"""

    assistant_msgs: list[AssistantMessage]
    tool_calls: list[ToolUseBlock]
    needs_follow_up: bool
    stop_reason: str | None = None
    withheld: str | None = None  # None | "prompt_too_long" | "max_output_tokens"
    yielded: list = Field(default_factory=list)  # 透传给外层的 Message | StreamEvent


async def stream_turn(state: State, params: "QueryParams", tracer: Tracer) -> StreamOutcome:
    """调 provider.stream → aggregate_stream → 填 StreamOutcome。

    needs_follow_up 只看 AssistantMessage 里有没有 ToolUseBlock,不看 stop_reason(红线#2)。
    withheld 检测留 # TODO Phase5(Phase 1 恒 None)。
    """
    max_tokens = state.max_output_tokens_override or params.max_tokens
    events = params.provider.stream(
        messages=state.messages,
        system=params.system,
        tools=params.tools,
        model=params.model,
        max_tokens=max_tokens,
        abort_signal=params.abort_signal,
        tracer=tracer,
    )
    assistant_msgs: list[AssistantMessage] = []
    tool_calls: list[ToolUseBlock] = []
    needs_follow_up = False
    stop_reason: str | None = None
    yielded: list = []
    async for item in aggregate_stream(events, tracer):
        yielded.append(item)
        if isinstance(item, AssistantMessage):
            assistant_msgs.append(item)
            stop_reason = item.stop_reason
            new_tools = [b for b in item.content if isinstance(b, ToolUseBlock)]
            if new_tools:  # ★ 只看 tool_use,不看 stop_reason
                tool_calls += new_tools
                needs_follow_up = True
    # TODO Phase5: 捕获 prompt_too_long / max_output_tokens → withheld
    return StreamOutcome(
        assistant_msgs=assistant_msgs,
        tool_calls=tool_calls,
        needs_follow_up=needs_follow_up,
        stop_reason=stop_reason,
        withheld=None,
        yielded=yielded,
    )
