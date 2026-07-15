"""phase: 流式调 LLM + aggregate_stream(边聚合边打点)。

aggregate_stream: 每个 content_block_stop 固化一个 block 就 yield 一条 block 级
AssistantMessage(content=[block]);message_stop 仅做 STREAM_END 埋点,不再组装整轮。
usage/stop_reason 仍在内部暂存供埋点用,整轮组装由 stream_turn 负责(见 Task 7)。
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
    from ...tool_executor.base import ToolExecutor
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
            idx = evt.index
            if idx is None:  # 守卫: 协议上 index 必有, 防御 pyright int|None
                continue
            blocks[idx] = dict(evt.block or {})
            if (evt.block or {}).get("type") == "tool_use":  # ★ TOOL_USE_DETECTED
                tracer.emit(
                    TraceEvent(
                        kind=TraceKind.TOOL_USE_DETECTED,
                        payload={
                            "tool_name": (evt.block or {}).get("name"),
                            "tool_use_id": (evt.block or {}).get("id"),
                            "index": idx,
                        },
                    )
                )
        elif evt.type == "content_block_delta":
            idx = evt.index
            if idx is None:  # 守卫: 防御 pyright int|None
                continue
            b = blocks[idx]
            d = evt.delta or {}
            if "text" in d:
                b["text"] = b.get("text", "") + d["text"]
            if "tool_input" in d:  # 累积 JSON 字符串,勿中途解析(红线#1)
                b["input_buf"] = b.get("input_buf", "") + d["tool_input"]
            if "thinking" in d:  # thinking_delta:累积思考内容(思考模型)
                b["thinking"] = b.get("thinking", "") + d["thinking"]
        elif evt.type == "content_block_stop":
            idx = evt.index
            if idx is None:  # 守卫: 防御 pyright int|None
                continue
            b = blocks[idx]
            if b.get("type") == "tool_use":
                try:
                    b["input"] = json.loads(b.pop("input_buf", "") or "{}")
                except json.JSONDecodeError:
                    # max_tokens 截断导致 input 残缺: 丢弃该 block, 由 stop_reason withhold 兜底
                    continue
            # block 级固化:每个 content_block_stop yield 一条只含该 block 的 AssistantMessage
            yield AssistantMessage(content=[_to_block(b)])
        elif evt.type == "message_delta":  # 暂存,仅供 STREAM_END 埋点(stream_turn 由 Task 7 独立取)
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
            # 不再组装整轮 yield(由 stream_turn 收集 block 级后组装,见 Task 7)


class StreamOutcome(BaseModel):
    """phase 之间传递的中间结果(避免 phase 直接改 state)。"""

    assistant_msgs: list[AssistantMessage]
    tool_calls: list[ToolUseBlock]
    needs_follow_up: bool
    stop_reason: str | None = None
    withheld: str | None = None  # None | "max_output_tokens" (prompt_too_long 走异常路径)
    yielded: list = Field(default_factory=list)  # 透传给外层的 Message | StreamEvent


async def stream_turn(
    state: State,
    params: "QueryParams",
    tracer: Tracer,
    executor: "ToolExecutor | None",
) -> StreamOutcome:
    """调 provider.stream → aggregate_stream → 喂 executor + 组装整轮 AssistantMessage。

    - StreamEvent: 透传到 yielded;message_delta 时取 stop_reason/usage;
    - block 级 AssistantMessage(aggregate_stream 每 content_block_stop 产一条):
      累积到 all_blocks;若是 ToolUseBlock 则 executor.add_tool(机会主义,block 一到即喂)
      + 收集 tool_calls + 置 needs_follow_up=True;block 级本身**不进 yielded**;
    - 遍历结束组装一条整轮 AssistantMessage(content=all_blocks, usage, stop_reason)
      追加到 yielded 末尾。

    needs_follow_up 只看有没有 ToolUseBlock,不看 stop_reason(红线#2)。
    withheld = "max_output_tokens" iff stop_reason == "max_tokens"(权威信号来自
    message_delta, 必到; prompt_too_long 走异常路径不产生 withheld)。
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
    all_blocks: list[TextBlock | ToolUseBlock] = []
    tool_calls: list[ToolUseBlock] = []
    needs_follow_up = False
    stop_reason: str | None = None
    usage = Usage()
    yielded: list = []
    async for item in aggregate_stream(events, tracer):
        if isinstance(item, StreamEvent):
            yielded.append(item)
            if item.type == "message_delta":  # 取 stop_reason/usage
                d = item.delta or {}
                if "stop_reason" in d:
                    stop_reason = d["stop_reason"]
                if item.message and "usage" in item.message:
                    usage = Usage(**item.message["usage"])
        else:  # block 级 AssistantMessage(内部累积,不进 yielded)
            block = item.content[0]
            all_blocks.append(block)
            if isinstance(block, ToolUseBlock):
                if executor is not None:  # 喂 executor(机会主义:block 一到即 add_tool)
                    executor.add_tool(block)
                tool_calls.append(block)
                needs_follow_up = True  # ★ 只看 tool_use,不看 stop_reason

    # 检测是否被截断: 权威信号是 stop_reason == "max_tokens"(来自 message_delta, 必到)
    withheld = None
    if stop_reason == "max_tokens":
        withheld = "max_output_tokens"

    # 组装整轮追加到 yielded 末尾(block 级不进 yielded,只发整轮)
    full = AssistantMessage(content=all_blocks, usage=usage, stop_reason=stop_reason)
    yielded.append(full)
    return StreamOutcome(
        assistant_msgs=[full],
        tool_calls=tool_calls,
        needs_follow_up=needs_follow_up,
        stop_reason=stop_reason,
        withheld=withheld,
        yielded=yielded,
    )
